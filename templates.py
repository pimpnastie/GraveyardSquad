# templates.py

DEFAULT_ROSTER_HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Graveyard Squad | Roster</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #0b0c10; color: #c5c6c7; font-family: 'Segoe UI', system-ui, sans-serif; padding-bottom: 50px; }
  .hero { background: #111418; padding: 40px 20px; text-align: center; border-bottom: 1px solid #1e2530; }
  .hero h1 { font-size: 2rem; color: #fff; font-weight: 700; margin-bottom: 14px; }
  .hero h1 span { color: #f1c40f; }
  .hero-btns { display: flex; gap: 12px; justify-content: center; flex-wrap: wrap; margin-bottom: 16px; }
  .btn { padding: 10px 22px; border-radius: 6px; font-weight: 700; font-size: 0.9rem; text-decoration: none; display: inline-block; transition: opacity 0.2s; }
  .btn:hover { opacity: 0.85; }
  .btn-green { background: #2ecc71; color: #0b0c10; }
  .btn-discord { background: #5865F2; color: #fff; }
  .hero-sub { font-size: 0.85rem; color: #6b7785; }
  .container { max-width: 900px; margin: 36px auto; padding: 0 20px; display: flex; gap: 28px; flex-wrap: wrap; }
  .main-col { flex: 2; min-width: 320px; }
  .side-col { flex: 1; min-width: 260px; }
  h2 { color: #fff; font-size: 1.1rem; font-weight: 700; margin-bottom: 14px; padding-bottom: 8px; border-bottom: 1px solid #1e2530; }
  .player-card { background: #161b22; border: 1px solid #1e2530; border-radius: 10px; padding: 14px 18px; margin-bottom: 10px; display: flex; align-items: center; justify-content: space-between; text-decoration: none; transition: border-color 0.2s, transform 0.15s; gap: 12px; flex-wrap: wrap; }
  .player-card:hover { border-color: #45a29e; transform: translateY(-1px); }
  .p-left { display: flex; flex-direction: column; gap: 3px; }
  .p-name { font-size: 1rem; font-weight: 700; color: #fff; }
  .p-role { font-size: 0.75rem; color: #6b7785; }
  .p-right { display: flex; flex-direction: column; align-items: flex-end; gap: 5px; }
  .p-trophies { font-size: 1rem; font-weight: 700; color: #f1c40f; }
  .p-stats-row { display: flex; gap: 12px; font-size: 0.75rem; color: #6b7785; }
  .p-stats-row span { color: #a0aab5; }
  .hof-card { background: #161b22; border: 1px solid #1e2530; border-left: 3px solid var(--hof-color, #45a29e); border-radius: 8px; padding: 14px 16px; margin-bottom: 10px; }
  .hof-label { font-size: 0.7rem; text-transform: uppercase; letter-spacing: 1px; color: #6b7785; margin-bottom: 4px; font-weight: 700; }
  .hof-name { font-size: 1rem; color: #fff; font-weight: 700; margin-bottom: 2px; }
  .hof-stat { font-size: 0.85rem; color: var(--hof-color, #45a29e); font-weight: 600; }
  .error-banner { background: #e74c3c; color: white; padding: 10px; text-align: center; font-weight: bold; margin-bottom: 20px; border-radius: 5px;}
</style>
</head>
<body>

<header class="hero">
  <h1>🛡️ <span>Graveyard</span> Clan Roster</h1>
  <div class="hero-btns">
    {% if session.get('discord_id') %}
      {% if session.get('discord_id') == '751975709643112569' %}
        <a href="/admin" class="btn btn-green">💀 HQ Control Panel</a>
      {% endif %}
      <a href="/logout" class="btn btn-discord">Logout (@{{ session.discord_name }})</a>
    {% else %}
      <a href="/login" class="btn btn-discord">Log in with Discord</a>
    {% endif %}
  </div>
  <div class="hero-sub">{{ players | length }} members &middot; Click a name to view their profile</div>
</header>

<div class="container">
  <div class="main-col">
    {% if error %}
      <div class="error-banner">⚠️ Failed to load live API data: {{ error }}</div>
    {% endif %}
    
    {% for p in players %}
    <a href="/player/{{ p.clean_tag }}" class="player-card">
      <div class="p-left">
        <div class="p-name cr-name">{{ p.name }}</div>
        <div class="p-role">
          {% if p.role == 'leader' %}Leader
          {% elif p.role == 'coLeader' %}CoLeader
          {% elif p.role == 'elder' %}Elder
          {% else %}Member{% endif %}
        </div>
      </div>
      <div class="p-right">
        <div class="p-trophies">🏆 {{ p.trophies }}</div>
        <div class="p-stats-row">
          <div>⭐ <span>{{ p.fame | default(0) }}</span></div>
          <div>🔥 <span>{{ p.current_streak | default(0) }}</span></div>
          <div>⚔️ <span>{{ p.warDayWins | default(0) }}</span></div>
          <div>🎁 <span>{{ p.donations | default(0) }}</span></div>
        </div>
      </div>
    </a>
    {% endfor %}
  </div>

  <div class="side-col">
    <h2>Hall of Fame</h2>
    <div class="hof-card" style="--hof-color: #3498db;">
      <div class="hof-label">Top Pusher</div>
      <div class="hof-name cr-name">{{ top_pusher.name if top_pusher else 'N/A' }}</div>
      <div class="hof-stat">🏆 {{ top_pusher.trophies if top_pusher else 0 }} Trophies</div>
    </div>
    <div class="hof-card" style="--hof-color: #e74c3c;">
      <div class="hof-label">Highest Win Streak</div>
      <div class="hof-name cr-name">{{ top_streak.name if top_streak else 'N/A' }}</div>
      <div class="hof-stat">🔥 {{ top_streak.current_streak if top_streak else 0 }} Wins</div>
    </div>
    <div class="hof-card" style="--hof-color: #f1c40f;">
      <div class="hof-label">War Legend</div>
      <div class="hof-name cr-name">{{ top_war.name if top_war else 'N/A' }}</div>
      <div class="hof-stat">⚔️ {{ top_war.warDayWins if top_war else 0 }} Lifetime Wins</div>
    </div>
  </div>
</div>

<script>
  document.querySelectorAll('.cr-name').forEach(el => {
    el.innerHTML = el.innerHTML.replace(/<c\d+>|<\/c>/gi, '');
  });
</script>
</body>
</html>
"""

DEFAULT_LINK_HTML = r"""
<!DOCTYPE html>
<html>
<head>
    <title>Link Account</title>
    <style>
        body { background: #121212; color: white; font-family: 'Segoe UI', sans-serif; text-align: center; padding: 50px; }
        .box { background: #1e1e1e; padding: 40px; border-radius: 10px; max-width: 400px; margin: auto; border: 1px solid #333; }
        h2 { color: #f1c40f; margin-bottom: 10px; }
        input { width: 100%; padding: 12px; margin: 15px 0; background: #2a2a2a; border: 1px solid #444; color: white; border-radius: 5px; font-size: 1rem;}
        button { width: 100%; background: #5865F2; color: white; padding: 12px; border: none; border-radius: 5px; font-weight: bold; cursor: pointer; font-size: 1rem;}
        button:hover { background: #4752C4; }
        .error { color: #e74c3c; margin-bottom: 15px; font-weight: bold; }
    </style>
</head>
<body>
    <div class="box">
        <h2>Link Clash Royale Tag</h2>
        <p style="color: #aaa; margin-bottom: 20px;">Authenticated as <strong>@{{ name }}</strong></p>
        {% if error %}<div class="error">{{ error }}</div>{% endif %}
        <form method="POST">
            <input type="text" name="tag" placeholder="e.g. #2Y8JLYPQ2" required>
            <button type="submit">Link to Discord</button>
        </form>
    </div>
</body>
</html>
"""

DEFAULT_PLAYER_HTML = r"""
<!DOCTYPE html>
<html>
<head>
    <title>{{ data.name }} - Analytics</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { background: #0f0f0f; color: #eee; font-family: 'Segoe UI', sans-serif; padding: 40px 30px; max-width: 1000px; margin: auto; }
        a.back { color: #f1c40f; text-decoration: none; font-weight: bold; font-size: 0.9rem; }
        a.back:hover { text-decoration: underline; }
        .header { border-bottom: 2px solid #f1c40f; padding-bottom: 14px; margin: 20px 0 30px; display: flex; justify-content: space-between; align-items: flex-end; flex-wrap: wrap; gap: 10px; }
        .header h1 { font-size: 1.8rem; }
        .header .tag { color: #5dade2; font-size: 1rem; font-weight: normal; margin-left: 8px; }
        .header .clan-badge { background: #1e1e1e; border: 1px solid #333; border-radius: 6px; padding: 6px 14px; font-size: 0.85rem; color: #ccc; }
        .header .clan-badge strong { color: #f1c40f; }
        h2 { color: #f1c40f; margin: 30px 0 14px; font-size: 1.1rem; text-transform: uppercase; letter-spacing: 1px; }
        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 14px; }
        .stat-box { background: #1a1a1a; padding: 18px 20px; border-radius: 10px; border: 1px solid #2a2a2a; }
        .label { color: #888; font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 6px; }
        .value { font-size: 1.6rem; font-weight: bold; color: #f1c40f; }
        .value.blue  { color: #5dade2; }
        .value.green { color: #2ecc71; }
        .value.red   { color: #e74c3c; }
        .value.white { color: #eee; }
        .deck-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; }
        @media (max-width: 600px) { .deck-grid { grid-template-columns: repeat(2, 1fr); } }
        .card-box { background: #1a1a1a; border: 1px solid #2a2a2a; border-radius: 8px; padding: 14px 10px; text-align: center; }
        .card-box .card-name { font-size: 0.82rem; font-weight: bold; margin-bottom: 5px; }
        .card-box .card-level { display: inline-block; background: #2a2a2a; color: #aaa; font-size: 0.75rem; border-radius: 4px; padding: 2px 7px; }
        .card-box.maxed { border-color: #f1c40f !important; }
        .card-box.maxed .card-level { color: #f1c40f; }
    </style>
</head>
<body>
    <a class="back" href="/">← Back to Roster</a>
    <div class="header">
        <div><h1>{{ data.name }}<span class="tag">{{ data.tag }}</span></h1></div>
        {% if data.clan %}
            <div class="clan-badge">🛡️ <strong>{{ data.clan.name }}</strong> &nbsp;·&nbsp; {{ data.role | replace('_', ' ') | title }}</div>
        {% else %}
            <div class="clan-badge">No Clan</div>
        {% endif %}
    </div>

    <h2>📈 Progression</h2>
    <div class="grid">
        <div class="stat-box"><div class="label">XP Level</div><div class="value white">⭐ {{ data.expLevel }}</div></div>
        <div class="stat-box"><div class="label">Current Trophies</div><div class="value blue">🏆 {{ data.trophies }}</div></div>
        <div class="stat-box"><div class="label">Best Trophies</div><div class="value blue">🏅 {{ data.bestTrophies }}</div></div>
        <div class="stat-box"><div class="label">Arena</div><div class="value white" style="font-size:1rem; padding-top:4px;">{{ data.arena.name if data.arena else '—' }}</div></div>
    </div>

    <h2>⚔️ Battle Stats</h2>
    <div class="grid">
        <div class="stat-box"><div class="label">Total Wins</div><div class="value green">{{ data.wins }}</div></div>
        <div class="stat-box"><div class="label">Losses</div><div class="value red">{{ data.losses }}</div></div>
        <div class="stat-box"><div class="label">3-Crown Wins</div><div class="value green">👑 {{ data.threeCrownWins }}</div></div>
        <div class="stat-box"><div class="label">Total Battles</div><div class="value white">{{ data.battleCount }}</div></div>
        <div class="stat-box">
            <div class="label">Current Win Streak</div>
            <div class="value {% if data.current_streak > 4 %}green{% elif data.current_streak > 0 %}white{% else %}red{% endif %}">
                🔥 {{ data.current_streak | default(0) }}
            </div>
        </div>
        <div class="stat-box">
            <div class="label">Win Rate</div>
            {% set total = data.wins + data.losses %}
            <div class="value {% if total > 0 and (data.wins / total * 100) >= 50 %}green{% else %}red{% endif %}">
                {% if total > 0 %}{{ "%.1f" | format(data.wins / total * 100) }}%{% else %}—{% endif %}
            </div>
        </div>
    </div>

    <h2>🎁 Social & Misc</h2>
    <div class="grid">
        <div class="stat-box"><div class="label">Donations (Season)</div><div class="value white">{{ data.donations | default(0) }}</div></div>
        <div class="stat-box"><div class="label">Donations Received</div><div class="value white">{{ data.donationsReceived | default(0) }}</div></div>
        <div class="stat-box"><div class="label">War Day Wins</div><div class="value white">⚔️ {{ data.warDayWins | default(0) }}</div></div>
        <div class="stat-box"><div class="label">3-Crown Win Rate</div>
            <div class="value {% if data.wins > 0 and (data.threeCrownWins / data.wins * 100) >= 30 %}green{% else %}white{% endif %}">
                {% if data.wins > 0 %}{{ "%.1f" | format(data.threeCrownWins / data.wins * 100) }}%{% else %}—{% endif %}
            </div>
        </div>
        <div class="stat-box"><div class="label">Favourite Card</div><div class="value white" style="font-size:1rem; padding-top:4px;">{{ data.currentFavouriteCard.name if data.currentFavouriteCard else '—' }}</div></div>
    </div>

    <h2>🃏 Current Battle Deck</h2>
    <div class="deck-grid">
        {% for card in data.currentDeck %}
            <div class="card-box {% if card.level >= max_lvl %}maxed{% endif %}">
                <div class="card-name">{{ card.name }}</div>
                <span class="card-level">Lvl {{ card.level }}{% if card.level >= max_lvl %} ✓{% endif %}</span>
            </div>
        {% endfor %}
    </div>

    <h2>⚔️ Recent Battles</h2>
    <style>
        .battle-row { display: flex; align-items: center; justify-content: space-between; background: #1a1a1a; border: 1px solid #2a2a2a; border-radius: 8px; padding: 12px 16px; margin-bottom: 8px; flex-wrap: wrap; gap: 8px; }
        .battle-row.win  { border-left: 3px solid #2ecc71; }
        .battle-row.loss { border-left: 3px solid #e74c3c; }
        .battle-row.draw { border-left: 3px solid #888; }
        .battle-type { font-size: 0.72rem; color: #888; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 2px; }
        .battle-opp  { font-size: 0.95rem; font-weight: bold; color: #eee; }
        .battle-score { font-size: 1.1rem; font-weight: bold; }
        .battle-score.win  { color: #2ecc71; }
        .battle-score.loss { color: #e74c3c; }
        .battle-score.draw { color: #aaa; }
        .battle-time { font-size: 0.75rem; color: #555; }
        .no-battles { color: #555; font-style: italic; padding: 20px 0; }
    </style>
    <div id="battles-section">
        <div class="no-battles">Loading recent battles...</div>
    </div>

    <script>
    async function loadPlayerBattles() {
        const tag = {{ data.tag | tojson }};
        const cleanTag = tag.replace('#', '');
        const section = document.getElementById('battles-section');
        try {
            const res = await fetch(`/api/player/${cleanTag}/battles`);
            const battles = await res.json();
            if (!battles.length) {
                section.innerHTML = '<div class="no-battles">No battles found. Run a harvest to populate battle history.</div>';
                return;
            }
            section.innerHTML = battles.map(b => {
                const cls = b.result || 'draw';
                const time = b.battle_time ? b.battle_time.replace('T', ' ').substring(0, 16) : '—';
                return `<div class="battle-row ${cls}">
                    <div>
                        <div class="battle-type">${b.type || 'PvP'}</div>
                        <div class="battle-opp">vs ${b.opp_name || 'Unknown'} <span style="color:#555; font-size:0.8rem;">${b.opp_tag || ''}</span></div>
                        <div class="battle-time">${time}</div>
                    </div>
                    <div class="battle-score ${cls}">
                        ${b.team_crowns ?? '—'} – ${b.opp_crowns ?? '—'}
                        <div style="font-size:0.8rem; font-weight:normal; text-align:right;">${cls.toUpperCase()}</div>
                    </div>
                </div>`;
            }).join('');
        } catch(e) {
            section.innerHTML = `<div class="no-battles">Error loading battle data. Make sure backend endpoint is deployed.</div>`;
        }
    }
    document.addEventListener('DOMContentLoaded', loadPlayerBattles);
    </script>
</body>
</html>
"""

DEFAULT_ADMIN_HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Graveyard HQ</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Barlow+Condensed:wght@400;600;700&display=swap" rel="stylesheet">
<style>
  :root {
    --bg:       #080a0f;
    --surface:  #0d1117;
    --panel:    #111820;
    --border:   #1e2d3d;
    --accent:   #00e5ff;
    --accent2:  #ff3d71;
    --ok:       #00e096;
    --warn:     #ffaa00;
    --err:      #ff3d71;
    --text:     #c9d1d9;
    --dim:      #4a5568;
    --font-mono: 'Share Tech Mono', monospace;
    --font-ui:   'Barlow Condensed', sans-serif;
  }
 
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: var(--font-ui); font-size: 15px; min-height: 100vh; display: flex; flex-direction: column; }
  
  /* ── TOP BAR ── */
  .topbar { display: flex; align-items: center; gap: 16px; padding: 12px 24px; background: var(--surface); border-bottom: 1px solid var(--border); position: sticky; top: 0; z-index: 100; }
  .topbar-title { font-size: 22px; font-weight: 700; letter-spacing: 2px; text-transform: uppercase; color: var(--accent); text-shadow: 0 0 18px rgba(0,229,255,0.35); flex: 1; }
  .topbar-title span { color: var(--dim); font-weight: 400; }
  .topbar-badge { font-family: var(--font-mono); font-size: 11px; padding: 3px 10px; border-radius: 3px; background: rgba(0,229,255,0.08); border: 1px solid var(--accent); color: var(--accent); letter-spacing: 1px; }
  .topbar a { color: var(--dim); text-decoration: none; font-size: 13px; letter-spacing: 1px; text-transform: uppercase; transition: color .2s; }
  .topbar a:hover { color: var(--text); }
 
  /* ── LAYOUT ── */
  .shell { display: flex; flex: 1; height: calc(100vh - 53px); }
  .sidebar { width: 200px; background: var(--surface); border-right: 1px solid var(--border); padding: 20px 0; flex-shrink: 0; display: flex; flex-direction: column; gap: 2px; }
  .nav-section { font-family: var(--font-mono); font-size: 10px; letter-spacing: 2px; color: var(--dim); padding: 14px 20px 6px; text-transform: uppercase; }
  .nav-btn { display: flex; align-items: center; gap: 10px; padding: 10px 20px; background: none; border: none; color: var(--dim); font-family: var(--font-ui); font-size: 14px; font-weight: 600; letter-spacing: .5px; text-transform: uppercase; cursor: pointer; text-align: left; width: 100%; border-left: 3px solid transparent; transition: all .15s; }
  .nav-btn:hover { color: var(--text); background: rgba(255,255,255,0.03); }
  .nav-btn.active { color: var(--accent); border-left-color: var(--accent); background: rgba(0,229,255,0.06); }
  .nav-icon { font-size: 16px; width: 20px; text-align: center; }
  .main { flex: 1; overflow-y: auto; padding: 28px 32px; }
  .tab-pane { display: none; }
  .tab-pane.active { display: block; }
 
  /* ── PAGE HEADER ── */
  .page-header { display: flex; align-items: baseline; gap: 14px; margin-bottom: 24px; border-bottom: 1px solid var(--border); padding-bottom: 14px; }
  .page-title { font-size: 28px; font-weight: 700; letter-spacing: 1px; text-transform: uppercase; color: #fff; }
  .page-sub { font-family: var(--font-mono); font-size: 12px; color: var(--dim); letter-spacing: 1px; }
 
  /* ── COMPONENTS ── */
  .stat-row { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 14px; margin-bottom: 28px; }
  .stat-card { background: var(--panel); border: 1px solid var(--border); border-radius: 6px; padding: 16px 18px; position: relative; overflow: hidden; }
  .stat-card::before { content: ''; position: absolute; top: 0; left: 0; right: 0; height: 2px; background: var(--accent); opacity: .6; }
  .stat-card.ok::before  { background: var(--ok);   }
  .stat-card.warn::before{ background: var(--warn);  }
  .stat-card.err::before { background: var(--err);   }
  .stat-label { font-family: var(--font-mono); font-size: 10px; letter-spacing: 2px; color: var(--dim); text-transform: uppercase; margin-bottom: 8px; }
  .stat-value { font-family: var(--font-mono); font-size: 26px; font-weight: 700; color: #fff; line-height: 1; }
  .stat-value.ok   { color: var(--ok);   }
  .stat-value.warn { color: var(--warn); }
  .stat-value.err  { color: var(--err);  }
  .stat-note { font-size: 11px; color: var(--dim); margin-top: 5px; }
  .diag-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(340px, 1fr)); gap: 16px; margin-bottom: 28px; }
  .diag-card { background: var(--panel); border: 1px solid var(--border); border-radius: 6px; overflow: hidden; }
  .diag-card-header { display: flex; align-items: center; justify-content: space-between; padding: 12px 16px; border-bottom: 1px solid var(--border); background: rgba(255,255,255,0.02); }
  .diag-card-title { font-size: 13px; font-weight: 700; letter-spacing: 1.5px; text-transform: uppercase; color: #fff; }
  .status-pill { font-family: var(--font-mono); font-size: 10px; padding: 3px 9px; border-radius: 20px; letter-spacing: 1px; text-transform: uppercase; font-weight: 700; }
  .pill-ok   { background: rgba(0,224,150,0.12); color: var(--ok);   border: 1px solid rgba(0,224,150,0.3); }
  .pill-warn { background: rgba(255,170,0,0.12);  color: var(--warn); border: 1px solid rgba(255,170,0,0.3); }
  .pill-err  { background: rgba(255,61,113,0.12); color: var(--err);  border: 1px solid rgba(255,61,113,0.3); }
  .pill-loading { background: rgba(255,255,255,0.05); color: var(--dim); border: 1px solid var(--border); }
  .diag-body { padding: 14px 16px; }
  .diag-row { display: flex; align-items: center; justify-content: space-between; padding: 6px 0; border-bottom: 1px solid rgba(255,255,255,0.04); font-family: var(--font-mono); font-size: 12px; }
  .diag-row:last-child { border-bottom: none; }
  .diag-key   { color: var(--dim); }
  .diag-val   { color: var(--text); text-align: right; }
  .diag-val.ok   { color: var(--ok);   }
  .diag-val.warn { color: var(--warn); }
  .diag-val.err  { color: var(--err);  }
  .section-label { font-family: var(--font-mono); font-size: 10px; letter-spacing: 3px; text-transform: uppercase; color: var(--dim); margin-bottom: 12px; margin-top: 24px; display: flex; align-items: center; gap: 10px; }
  .section-label::after { content: ''; flex: 1; height: 1px; background: var(--border); }
  .toolbar { display: flex; align-items: center; gap: 12px; margin-bottom: 24px; }
  
  /* Buttons */
  .btn-refresh { display: flex; align-items: center; gap: 8px; padding: 8px 18px; background: rgba(0,229,255,0.08); border: 1px solid var(--accent); border-radius: 4px; color: var(--accent); font-family: var(--font-ui); font-weight: 700; font-size: 13px; letter-spacing: 1px; text-transform: uppercase; cursor: pointer; transition: all .2s; }
  .btn-refresh:hover { background: rgba(0,229,255,0.16); }
  .btn-danger { display: flex; align-items: center; gap: 8px; padding: 8px 18px; background: rgba(255,61,113,0.08); border: 1px solid var(--err); border-radius: 4px; color: var(--err); font-family: var(--font-ui); font-weight: 700; font-size: 13px; letter-spacing: 1px; text-transform: uppercase; cursor: pointer; transition: all .2s; }
  .btn-danger:hover { background: rgba(255,61,113,0.16); }
  .last-refresh { font-family: var(--font-mono); font-size: 11px; color: var(--dim); margin-left: auto; }
  @keyframes spin { to { transform: rotate(360deg); } }
  .spin { display: inline-block; animation: spin .8s linear infinite; }
 
  /* Table & Inputs */
  .war-table { width: 100%; border-collapse: collapse; font-family: var(--font-mono); font-size: 12px; }
  .war-table th { text-align: left; font-size: 10px; letter-spacing: 2px; text-transform: uppercase; color: var(--dim); padding: 8px 12px; border-bottom: 1px solid var(--border); }
  .war-table td { padding: 9px 12px; border-bottom: 1px solid rgba(255,255,255,0.04); color: var(--text); }
  .war-table tr:hover td { background: rgba(255,255,255,0.03); }
  .form-input, .form-select { padding: 8px 12px; background: #050709; color: #fff; border: 1px solid var(--border); border-radius: 4px; font-family: var(--font-mono); }
  .toast-wrap { position: fixed; bottom: 24px; right: 24px; display: flex; flex-direction: column; gap: 8px; z-index: 9999; }
  .toast { padding: 10px 18px; border-radius: 5px; font-family: var(--font-mono); font-size: 12px; border: 1px solid; animation: fadeIn .25s ease; cursor: pointer; }
  @keyframes fadeIn { from { opacity:0; transform:translateY(8px); } to { opacity:1; transform:translateY(0); } }
  .toast-ok   { background: rgba(0,224,150,0.1); border-color: var(--ok);  color: var(--ok);  }
  .toast-err  { background: rgba(255,61,113,0.1); border-color: var(--err); color: var(--err); }
  .toast-info { background: rgba(0,229,255,0.1);  border-color: var(--accent); color: var(--accent); }
  ::-webkit-scrollbar { width: 6px; height: 6px; }
  ::-webkit-scrollbar-track { background: var(--bg); }
  ::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }
  .log-box { background: #050709; border: 1px solid var(--border); border-radius: 6px; padding: 14px; font-family: var(--font-mono); font-size: 11px; color: var(--dim); max-height: 240px; overflow-y: auto; line-height: 1.7; white-space: pre-wrap; word-break: break-all; }
  .log-line-ok   { color: var(--ok);   }
  .log-line-warn { color: var(--warn); }
  .log-line-err  { color: var(--err);  }
</style>
</head>
<body>
 
<header class="topbar">
  <div class="topbar-title">☠ Graveyard <span>HQ</span></div>
  <span class="topbar-badge" id="clan-tag-badge">CLAN: #{{ clan_tag }}</span>
  <a href="/">← Roster</a>
  <a href="/logout">Logout</a>
</header>
 
<div class="shell">
  <nav class="sidebar">
    <div class="nav-section">Navigation</div>
    <button class="nav-btn active" onclick="showTab('diag', this)"><span class="nav-icon">🔍</span>Diagnostics</button>
    <button class="nav-btn" onclick="showTab('war', this)"><span class="nav-icon">⚔️</span>War Monitor</button>
    <button class="nav-btn" onclick="showTab('battles', this)"><span class="nav-icon">📜</span>Battle Logs</button>
    <button class="nav-btn" onclick="showTab('harvest', this)"><span class="nav-icon">📡</span>Harvest Log</button>
    <button class="nav-btn" onclick="showTab('csv', this)"><span class="nav-icon">📄</span>CSV Export</button>
    <button class="nav-btn" onclick="showTab('editor', this)"><span class="nav-icon">🎨</span>UI Editor</button>
    <div class="nav-section">Danger Zone</div>
    <button class="nav-btn" onclick="showTab('admin', this)"><span class="nav-icon">⚙️</span>Admin Tools</button>
  </nav>
 
  <main class="main">
    
    <div class="tab-pane active" id="tab-diag">
      <div class="page-header">
        <div class="page-title">Diagnostics</div>
        <div class="page-sub" id="diag-env">Initializing...</div>
      </div>
      <div class="toolbar">
        <button class="btn-refresh" id="btn-diag-refresh" onclick="loadDiagnostics()"><span id="diag-spin">↻</span> Refresh</button>
        <span class="last-refresh" id="diag-last-refresh">Never refreshed</span>
      </div>
      <div class="stat-row" id="stat-row">
        <div class="stat-card" id="sc-redis"><div class="stat-label">Redis</div><div class="stat-value">—</div><div class="stat-note">Checking...</div></div>
        <div class="stat-card" id="sc-mongo"><div class="stat-label">MongoDB</div><div class="stat-value">—</div><div class="stat-note">Checking...</div></div>
        <div class="stat-card" id="sc-crapi"><div class="stat-label">CR API</div><div class="stat-value">—</div><div class="stat-note">Checking...</div></div>
        <div class="stat-card" id="sc-cache-keys"><div class="stat-label">Cache Keys</div><div class="stat-value">—</div><div class="stat-note">Redis key count</div></div>
        <div class="stat-card" id="sc-harvest"><div class="stat-label">Last Harvest</div><div class="stat-value" style="font-size:15px">—</div><div class="stat-note">Snapshot timestamp</div></div>
      </div>
      
      <div class="section-label">Infrastructure</div>
      <div class="diag-grid">
        <div class="diag-card"><div class="diag-card-header"><div class="diag-card-title">⚡ Redis</div><span class="status-pill pill-loading" id="pill-redis">LOADING</span></div><div class="diag-body" id="body-redis"></div></div>
        <div class="diag-card"><div class="diag-card-header"><div class="diag-card-title">🍃 MongoDB</div><span class="status-pill pill-loading" id="pill-mongo">LOADING</span></div><div class="diag-body" id="body-mongo"></div></div>
        <div class="diag-card"><div class="diag-card-header"><div class="diag-card-title">🃏 CR API</div><span class="status-pill pill-loading" id="pill-crapi">LOADING</span></div><div class="diag-body" id="body-crapi"></div></div>
        <div class="diag-card"><div class="diag-card-header"><div class="diag-card-title">🤖 Bot Process</div><span class="status-pill pill-loading" id="pill-bot">LOADING</span></div><div class="diag-body" id="body-bot"></div></div>
      </div>
      <div class="section-label">Cache & Data</div>
      <div class="diag-grid">
        <div class="diag-card"><div class="diag-card-header"><div class="diag-card-title">📊 Cache Stats</div></div><div class="diag-body" id="body-cache"></div></div>
        <div class="diag-card"><div class="diag-card-header"><div class="diag-card-title">⏳ Tasks</div></div><div class="diag-body" id="body-tasks"></div></div>
      </div>
      <div class="section-label">Event Log</div>
      <div class="log-box" id="diag-log">Waiting for data...\n</div>
    </div>
 
    <div class="tab-pane" id="tab-war">
      <div class="page-header"><div class="page-title">War Monitor</div><div class="page-sub">Current River Race</div></div>
      <div class="toolbar"><button class="btn-refresh" onclick="loadWar()">↻ Refresh</button><span class="last-refresh" id="war-last-refresh"></span></div>
      <div id="war-content"><div style="color:var(--dim); font-family:var(--font-mono); font-size:12px;">Click refresh to load war data.</div></div>
    </div>
 
    <div class="tab-pane" id="tab-harvest">
      <div class="page-header">
        <div class="page-title">Harvest Log</div>
        <div class="page-sub">Historical Data & Manual Triggers</div>
      </div>
      <div class="toolbar">
        <button class="btn-danger" onclick="triggerManualHarvest()" style="background: rgba(241, 196, 15, 0.1); border-color: #f1c40f; color: #f1c40f;">
          ⚡ Force Manual Snapshot Overwrite
        </button>
        <button class="btn-refresh" onclick="loadDiagnostics()">↻ Refresh Status</button>
      </div>
      <div class="diag-grid">
        <div class="diag-card">
          <div class="diag-card-header"><div class="diag-card-title">📡 Current Harvest Info</div></div>
          <div class="diag-body" id="harvest-detail-body">Load diagnostics first.</div>
        </div>
        <div class="diag-card">
          <div class="diag-card-header"><div class="diag-card-title">📅 Snapshot History Dates</div></div>
          <div class="diag-body" id="harvest-history-body" style="max-height: 200px; overflow-y: auto;">Load diagnostics first.</div>
        </div>
      </div>
    </div>
    
    <div class="tab-pane" id="tab-csv">
      <div class="page-header">
        <div class="page-title">Data Exporter</div>
        <div class="page-sub">Generate Custom CSV & Computed Logic</div>
      </div>
      <div class="diag-card" style="padding: 24px; background: var(--panel);">
        <form id="csv-export-form" onsubmit="handleCustomCSVExport(event)">
            <label style="color: var(--dim); font-family: var(--font-mono); font-size: 12px; text-transform: uppercase;">1. Select Base Database Fields</label><br>
<div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; margin: 12px 0 24px; color: #fff; font-family: var(--font-mono); font-size: 13px;">
    <label><input type="checkbox" name="fields" value="name" checked> Name</label>
    <label><input type="checkbox" name="fields" value="tag" checked> Tag</label>
    <label><input type="checkbox" name="fields" value="role" checked> Role</label>
    <label><input type="checkbox" name="fields" value="trophies" checked> Trophies</label>
    <label><input type="checkbox" name="fields" value="fame" checked> War Fame</label>
    <label><input type="checkbox" name="fields" value="totalWins"> Total Wins</label>
    <label><input type="checkbox" name="fields" value="totalLosses"> Total Losses</label>
    <label><input type="checkbox" name="fields" value="current_streak"> Win Streak</label>
    <label><input type="checkbox" name="fields" value="donations"> Donations</label>
    
    <label><input type="checkbox" name="fields" value="warDayWins"> War Day Wins</label>
    <label><input type="checkbox" name="fields" value="decksUsedToday" checked> Decks Used</label>
    <label><input type="checkbox" name="fields" value="decksRemaining" checked> Decks Remaining</label>
</div>
            
            <label style="color: var(--dim); font-family: var(--font-mono); font-size: 12px; text-transform: uppercase;">2. Auto-Computed Logic Formulas (JS Evaluated)</label><br>
            <div style="display: grid; grid-template-columns: 1fr; gap: 10px; margin: 12px 0 24px; color: #fff; font-family: var(--font-mono); font-size: 13px;">
                <label><input type="checkbox" id="formula-winrate"> <strong>Win Rate %</strong> <span style="color:var(--dim);">( totalWins / (totalWins + totalLosses) * 100 )</span></label>
                <label><input type="checkbox" id="formula-warpart"> <strong>War Participation %</strong> <span style="color:var(--dim);">( decksUsedToday / (decksUsedToday + decksRemaining) * 100 )</span></label>
            </div>
            
            <button type="submit" class="btn-refresh" style="display: inline-flex; border-color: var(--ok); color: var(--ok); background: rgba(0,224,150,0.08);">
              📥 Generate & Download CSV
            </button>
        </form>
      </div>
    </div>

    <div class="tab-pane" id="tab-editor">
      <div class="page-header">
        <div class="page-title">UI Editor</div>
        <div class="page-sub">Live deploy or Preview custom HTML</div>
      </div>
      <div class="diag-card" style="padding: 24px; background: var(--panel);">
        <div style="display: flex; gap: 12px; align-items: center; margin-bottom: 16px;">
            <select id="editor-template-name" class="form-select">
                <option value="roster">Roster (Home)</option>
                <option value="player">Player Profile</option>
                <option value="admin">Admin Dashboard</option>
                <option value="link">Discord Link Page</option>
            </select>
            <button onclick="fetchTemplateForEditor('current')" class="btn-refresh" style="padding: 6px 12px;">Load Live DB Version</button>
            <button onclick="fetchTemplateForEditor('default')" class="btn-refresh" style="padding: 6px 12px;">Load Factory Default</button>
        </div>
        
        <form action="/admin/update-html" method="POST" id="editor-form">
          <input type="hidden" name="template_name" id="hidden-template-name" value="roster">
          <textarea id="editor-html-content" name="html_content" rows="20" style="width: 100%; margin-bottom: 16px; padding: 16px; background: #050709; color: var(--accent); font-family: var(--font-mono); font-size: 13px; border: 1px solid var(--border); border-radius: 4px; line-height: 1.5;"></textarea><br>
          <div style="display: flex; gap: 12px;">
              <button type="submit" class="btn-refresh" style="border-color: var(--ok); color: var(--ok); background: rgba(0,224,150,0.08);">🚀 Deploy Live Update</button>
              <button type="button" onclick="previewTemplate()" class="btn-refresh" style="border-color: #f1c40f; color: #f1c40f; background: rgba(241, 196, 15, 0.1);">👀 Test / Preview Code</button>
          </div>
        </form>
      </div>
    </div>
 
    <div class="tab-pane" id="tab-admin">
      <div class="page-header"><div class="page-title">Admin Tools</div><div class="page-sub">Careful in here</div></div>
      <div class="diag-grid">
        <div class="diag-card">
          <div class="diag-card-header"><div class="diag-card-title">🔄 Cache Flush</div></div>
          <div class="diag-body" style="display:flex;flex-direction:column;gap:10px;">
            <p style="font-size:12px;color:var(--dim);font-family:var(--font-mono);">Flush all Redis cache keys for this clan. Use when data looks stale.</p>
            <button class="btn-danger" onclick="confirmFlushCache()">⚠ Flush CR Cache</button>
          </div>
        </div>
        <div class="diag-card">
          <div class="diag-card-header"><div class="diag-card-title">🩺 Health Check API</div></div>
          <div class="diag-body" style="display:flex;flex-direction:column;gap:10px;">
            <p style="font-size:12px;color:var(--dim);font-family:var(--font-mono);">Raw JSON payload of all internal diagnostics.</p>
            <button class="btn-refresh" onclick="window.open('/admin/diagnostics','_blank')">Open Raw JSON ↗</button>
          </div>
        </div>
      </div>
    </div>
    <div class="tab-pane" id="tab-battles">
      <div class="page-header">
        <div class="page-title">Battle Logs</div>
        <div class="page-sub">Raw Combat Feed from MongoDB</div>
      </div>
      <div class="toolbar">
        <button class="btn-refresh" onclick="loadBattles()">↻ Fetch Latest Logs</button>
      </div>
      <div class="diag-card" style="padding: 0; overflow-x: auto;">
        <table class="war-table" id="battles-table">
          <thead>
            <tr>
              <th>Time (UTC)</th>
              <th>Player</th>
              <th>Tag</th>
              <th>Type</th>
              <th>Result</th>
              <th>Score</th>
              <th>Opponent</th>
            </tr>
          </thead>
          <tbody id="battles-body">
            <tr><td colspan="7" style="text-align: center; padding: 24px; color: var(--dim);">Click fetch to load the latest 100 database records.</td></tr>
          </tbody>
        </table>
      </div>
    </div>
 
  </main>
</div>
 
<div class="toast-wrap" id="toast-wrap"></div>
 
<form id="preview-form" action="/admin/preview" method="POST" target="_blank" style="display:none;">
    <input type="hidden" name="template" id="preview-template-name">
    <textarea name="html" id="preview-html"></textarea>
</form>

<script>
async function loadBattles() {
    const tbody = document.getElementById('battles-body');
    tbody.innerHTML = '<tr><td colspan="7" style="text-align:center; padding:24px; color:var(--dim);"><span class="spin">↻</span> Loading...</td></tr>';
    try {
        const res = await fetch('/admin/api/battles');
        const battles = await res.json();
        if (!battles.length) {
            tbody.innerHTML = '<tr><td colspan="7" style="text-align:center; padding:24px; color:var(--dim);">No battles found in database.</td></tr>';
            return;
        }
        tbody.innerHTML = battles.map(b => {
            const resultCls = b.result === 'win' ? 'ok' : b.result === 'loss' ? 'err' : '';
            return `<tr>
                <td>${escHtml(b.battle_time || '—')}</td>
                <td>${escHtml(b.player_name || '—')}</td>
                <td>${escHtml(b.player_tag || '—')}</td>
                <td>${escHtml(b.type || '—')}</td>
                <td class="diag-val ${resultCls}">${escHtml(b.result || '—')}</td>
                <td>${b.team_crowns ?? '—'} – ${b.opp_crowns ?? '—'}</td>
                <td>${escHtml(b.opp_name || '—')}</td>
            </tr>`;
        }).join('');
        toast(`Loaded ${battles.length} battle records.`, 'ok');
    } catch(e) {
        tbody.innerHTML = `<tr><td colspan="7" style="color:var(--err); padding:24px;">${escHtml(e.message)}</td></tr>`;
        toast('Failed to load battles: ' + e.message, 'err');
    }
}
function showTab(name, btn) {
  document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
  const pane = document.getElementById('tab-' + name);
  if (pane) pane.classList.add('active');
  if (btn)  btn.classList.add('active');
}
function toast(msg, type='info', duration=3500) {
  const wrap = document.getElementById('toast-wrap');
  const t = document.createElement('div');
  t.className = `toast toast-${type}`;
  t.textContent = msg;
  t.onclick = () => t.remove();
  wrap.appendChild(t);
  setTimeout(() => t.remove(), duration);
}
const _logLines = [];
function appendLog(msg, level='info') {
  const ts = new Date().toLocaleTimeString();
  _logLines.push({ ts, msg, level });
  if (_logLines.length > 200) _logLines.shift();
  const box = document.getElementById('diag-log');
  box.innerHTML = _logLines.map(l => `<span class="log-line-${l.level}">[${l.ts}] ${escHtml(l.msg)}</span>`).join('\n');
  box.scrollTop = box.scrollHeight;
}
function escHtml(s) { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
function setPill(id, text, type) { const el = document.getElementById(id); if(el){ el.className=`status-pill pill-${type}`; el.textContent=text;} }
function setStatCard(id, value, note, status) {
  const card = document.getElementById(id);
  if (!card) return;
  card.className = `stat-card ${status}`;
  card.querySelector('.stat-value').className = `stat-value ${status}`;
  card.querySelector('.stat-value').textContent = value;
  card.querySelector('.stat-note').textContent  = note;
}
function renderRows(rows) {
  return rows.map(([k, v, cls='']) => `<div class="diag-row"><span class="diag-key">${escHtml(k)}</span><span class="diag-val ${cls}">${escHtml(String(v))}</span></div>`).join('');
}
async function fetchTemplateForEditor(source) {
    const name = document.getElementById('editor-template-name').value;
    document.getElementById('hidden-template-name').value = name;
    try {
        const res = await fetch(`/admin/api/template/${name}?source=${source}`);
        const data = await res.json();
        if(data.html !== undefined) {
            document.getElementById('editor-html-content').value = data.html;
            toast(`Loaded ${source} HTML for ${name}`, 'ok');
        } else {
            toast('Failed to load template source', 'err');
        }
    } catch(e) { toast('Error loading template', 'err'); }
}
function previewTemplate() {
    document.getElementById('preview-template-name').value = document.getElementById('editor-template-name').value;
    document.getElementById('preview-html').value = document.getElementById('editor-html-content').value;
    document.getElementById('preview-form').submit();
}
async function triggerManualHarvest() {
    if(!confirm("Force snapshot generation? This will execute the daily loop immediately and overwrite today's existing snapshot entry if it exists.")) return;
    try {
        const res = await fetch('/admin/harvest/manual', {method: 'POST'});
        const data = await res.json();
        toast(data.message, 'ok');
        appendLog('Manual harvest broadcast sent.', 'warn');
    } catch(e) { toast(e.message, 'err'); }
}
async function loadWar() {
    const content = document.getElementById('war-content');
    const lastRef = document.getElementById('war-last-refresh');
    content.innerHTML = '<div style="color:var(--dim);"><span class="spin">↻</span> Fetching live war data...</div>';
    try {
        const res = await fetch('/admin/api/war');
        const data = await res.json();
        if (data.error) throw new Error(data.error);
        const state = data.state || 'Unknown';
        const fame = data.clan ? data.clan.fame : 0;
        const participants = data.clan && data.clan.participants ? data.clan.participants.length : 0;
        content.innerHTML = `
            <div class="stat-row">
                <div class="stat-card ok"><div class="stat-label">Race State</div><div class="stat-value" style="font-size: 18px;">${state.toUpperCase()}</div><div class="stat-note">Current Phase</div></div>
                <div class="stat-card ok"><div class="stat-label">Clan Fame</div><div class="stat-value">⭐ ${fame}</div><div class="stat-note">Total Points</div></div>
                <div class="stat-card ok"><div class="stat-label">Participants</div><div class="stat-value">👥 ${participants}</div><div class="stat-note">Active this week</div></div>
            </div>
        `;
        lastRef.textContent = 'Last refresh: ' + new Date().toLocaleTimeString();
        toast('War data loaded successfully.', 'ok');
    } catch(err) {
        content.innerHTML = `<div style="color:var(--err); font-family:var(--font-mono);">Error: ${err.message}</div>`;
        toast('Failed to load war data', 'err');
    }
}
async function handleCustomCSVExport(e) {
    e.preventDefault();
    const form = e.target;
    const formData = new FormData(form);
    formData.set('export_format', 'json'); 
    toast('Fetching data for custom CSV computation...', 'info');
    try {
        const res = await fetch('/admin/export/custom', { method: 'POST', body: formData });
        let records = await res.json();
        if (!Array.isArray(records)) throw new Error("Invalid response format.");
        const wantWinRate = document.getElementById('formula-winrate').checked;
        const wantWarPart = document.getElementById('formula-warpart').checked;
        let headers = Object.keys(records[0] || {});
        if(wantWinRate) headers.push("Computed_WinRate%");
        if(wantWarPart) headers.push("Computed_WarParticipation%");
        let csvContent = headers.join(",") + "\\n";
        for(const row of records) {
            if(wantWinRate) {
                const w = row.totalWins || 0;
                const l = row.totalLosses || 0;
                row["Computed_WinRate%"] = (w+l > 0) ? ((w / (w+l)) * 100).toFixed(1) : 0;
            }
            if(wantWarPart) {
                const used = row.decksUsedToday || 0;
                const rem = row.decksRemaining || 4;
                const totalDecks = used + rem;
                row["Computed_WarParticipation%"] = (totalDecks > 0) ? ((used / totalDecks) * 100).toFixed(1) : 0;
            }
            let rowString = headers.map(h => {
                let val = row[h] !== null && row[h] !== undefined ? row[h] : "N/A";
                return `"${String(val).replace(/"/g, '""')}"`;
            }).join(",");
            csvContent += rowString + "\\n";
        }
        const blob = new Blob([csvContent], { type: 'text/csv;charset=utf-8;' });
        const link = document.createElement("a");
        const url = URL.createObjectURL(blob);
        link.setAttribute("href", url);
        link.setAttribute("download", "Graveyard_Custom_Export.csv");
        link.style.visibility = 'hidden';
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);
        toast('CSV successfully generated and downloaded!', 'ok');
    } catch(err) {
        toast('Error generating CSV: ' + err.message, 'err');
    }
}
async function loadDiagnostics() {
  const btn = document.getElementById('btn-diag-refresh');
  const spin = document.getElementById('diag-spin');
  btn.disabled = true; spin.className = 'spin';
  appendLog('Fetching /admin/diagnostics...', 'info');
  try {
    const resp = await fetch('/admin/diagnostics');
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const d = await resp.json();
    appendLog(`Response received (HTTP 200)`, 'ok');
    renderDiagnostics(d);
    document.getElementById('diag-last-refresh').textContent = 'Last refresh: ' + new Date().toLocaleTimeString();
  } catch(e) {
    appendLog(`Error: ${e.message}`, 'err');
    toast('Failed to load diagnostics: ' + e.message, 'err');
  } finally { btn.disabled = false; spin.className = ''; }
}
function renderDiagnostics(d) {
  document.getElementById('diag-env').textContent = `v${d.version || '?'} · ${d.environment || 'unknown'} · ${d.hostname || ''}`;
  const redis = d.redis || {}; const redisOk = redis.status === 'ok';
  setPill('pill-redis', redisOk ? 'ONLINE' : 'OFFLINE', redisOk ? 'ok' : 'err');
  setStatCard('sc-redis', redisOk ? '✓' : '✗', redis.ping_ms != null ? `${redis.ping_ms}ms ping` : 'unreachable', redisOk ? 'ok' : 'err');
  document.getElementById('body-redis').innerHTML = renderRows([['Status', redis.status||'unknown', redisOk?'ok':'err'],['Ping', redis.ping_ms!=null?redis.ping_ms+' ms':'N/A'],['Used Memory', redis.used_memory||'N/A'],['Total Keys', redis.total_keys??'N/A']]);
  const mongo = d.mongo || {}; const mongoOk = mongo.status === 'ok';
  setPill('pill-mongo', mongoOk ? 'ONLINE' : 'OFFLINE', mongoOk ? 'ok' : 'err');
  setStatCard('sc-mongo', mongoOk ? '✓' : '✗', mongo.ping_ms != null ? `${mongo.ping_ms}ms ping` : 'unreachable', mongoOk ? 'ok' : 'err');
  document.getElementById('body-mongo').innerHTML = renderRows([['Status', mongo.status||'unknown', mongoOk?'ok':'err'],['Ping', mongo.ping_ms!=null?mongo.ping_ms+' ms':'N/A'],['Snapshots', mongo.snapshot_count??'N/A'],['Battle Docs', mongo.battle_count??'N/A']]);
  const api = d.cr_api || {}; const apiOk = api.status === 'ok'; const apiCls = apiOk ? 'ok' : (api.status === 'rate_limited' ? 'warn' : 'err');
  setPill('pill-crapi', api.status?.toUpperCase() || 'UNKNOWN', apiCls);
  setStatCard('sc-crapi', api.status_code || '—', api.latency_ms != null ? `${api.latency_ms}ms` : 'unreachable', apiCls);
  document.getElementById('body-crapi').innerHTML = renderRows([['Status', api.status||'unknown', apiCls],['HTTP Code', api.status_code??'N/A'],['Latency', api.latency_ms!=null?api.latency_ms+' ms':'N/A'],['Endpoint', api.endpoint_tested||'N/A']]);
  const bot = d.bot || {}; const botOk = bot.connected === true;
  setPill('pill-bot', botOk ? 'CONNECTED' : 'OFFLINE', botOk ? 'ok' : 'err');
  document.getElementById('body-bot').innerHTML = renderRows([['Discord WS', bot.connected?'Connected':'Disconnected', bot.connected?'ok':'err'],['Latency', bot.latency_ms!=null?bot.latency_ms+' ms':'N/A'],['Uptime', bot.uptime||'N/A']]);
  const cache = d.cache || {}; const totalKeys = cache.total_keys ?? 0;
  setStatCard('sc-cache-keys', totalKeys, 'keys in store', totalKeys > 0 ? 'ok' : 'warn');
  document.getElementById('body-cache').innerHTML = renderRows([['Backend', cache.backend||'unknown'],['Total Keys', cache.total_keys??0],['HTML Cache', cache.html_cache_entries??0]]);
  const harv = d.harvest || {}; const harvOk = !!harv.last_run;
  setStatCard('sc-harvest', harv.last_run || 'Never', harv.snapshots_saved ? `${harv.snapshots_saved} snaps` : 'no data', harvOk ? 'ok' : 'warn');
  document.getElementById('harvest-detail-body').innerHTML = renderRows([
    ['Last Run', harv.last_run || 'Never'], ['Status', harv.status || 'unknown', harv.status === 'ok' ? 'ok' : 'warn'],
    ['Snapshots Saved', harv.snapshots_saved ?? 'N/A'], ['Battles Saved', harv.battles_saved ?? 'N/A']
  ]);
  const historyList = (harv.history_dates || []).map(date => `
    <div class="diag-row" style="padding: 10px 0;">
        <span class="diag-key" style="color: #fff;">${escHtml(date)}</span>
        <span class="diag-val">
            <button class="btn-refresh" 
                    style="padding: 6px 12px; font-size: 11px; border-color: var(--accent);" 
                    onclick="window.open('/admin/api/snapshot/${escHtml(date)}', '_blank')">
                👀 View Snapshot
            </button>
        </span>
    </div>
`).join('');
  document.getElementById('harvest-history-body').innerHTML = historyList || 'No historical snapshots found in DB.';
  const tasks = d.tasks || {};
  document.getElementById('body-tasks').innerHTML = renderRows([['Snapshot Loop', tasks.snapshot_loop||'unknown'], ['Next Snapshot', tasks.next_snapshot||'N/A']]);
  appendLog('Diagnostics render complete.', 'ok');
}
function confirmFlushCache() {
  if (!confirm('Flush all CR API cache keys?')) return;
  fetch('/admin/flush-cache', { method: 'POST' }).then(r=>r.json()).then(d=>{toast(d.message,'ok'); loadDiagnostics();}).catch(e=>toast(e.message,'err'));
}
document.addEventListener('DOMContentLoaded', () => { loadDiagnostics(); });
</script>
</body>
</html>
"""