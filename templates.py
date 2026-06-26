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
  .error-banner { background: #e74c3c; color: white; padding: 10px; text-align: center; font-weight: bold; margin-bottom: 20px; border-radius: 5px; }
</style>
</head>
<body>

<header class="hero">
  <h1>🛡️ <span>Graveyard</span> Clan Roster</h1>
  <div class="hero-btns">
      {% if session.get('discord_id') %}
        {% if session.get('is_admin_user') %}
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
      <div class="error-banner">⚠️ {{ error }}</div>
    {% endif %}

    {% for p in players %}
    <a href="/player/{{ p.clean_tag }}" class="player-card">
      <div class="p-left">
        <div class="p-name cr-name">{{ p.name }}</div>
        <div class="p-role">
          {% if p.role == 'leader' %}Leader
          {% elif p.role == 'coLeader' %}Co-Leader
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
    input { width: 100%; padding: 12px; margin: 15px 0; background: #2a2a2a; border: 1px solid #444; color: white; border-radius: 5px; font-size: 1rem; }
    button { width: 100%; background: #5865F2; color: white; padding: 12px; border: none; border-radius: 5px; font-weight: bold; cursor: pointer; font-size: 1rem; }
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
    .battle-row { display: flex; align-items: center; justify-content: space-between; background: #1a1a1a; border: 1px solid #2a2a2a; border-radius: 8px; padding: 12px 16px; margin-bottom: 8px; flex-wrap: wrap; gap: 8px; }
    .battle-row.win  { border-left: 3px solid #2ecc71; }
    .battle-row.loss { border-left: 3px solid #e74c3c; }
    .battle-row.draw { border-left: 3px solid #888; }
    .battle-type { font-size: 0.72rem; color: #888; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 2px; }
    .battle-opp  { font-size: 0.95rem; font-weight: bold; color: #eee; }
    .battle-score { font-size: 1.1rem; font-weight: bold; text-align: right; }
    .battle-score.win  { color: #2ecc71; }
    .battle-score.loss { color: #e74c3c; }
    .battle-score.draw { color: #aaa; }
    .battle-time { font-size: 0.75rem; color: #555; }
    .no-battles { color: #555; font-style: italic; padding: 20px 0; }
  </style>
</head>
<body>

  <a class="back" href="/">← Back to Roster</a>

  <div class="header">
    <div>
      <h1>{{ data.name }}<span class="tag">{{ data.tag }}</span></h1>
    </div>
    {% if data.clan %}
      <div class="clan-badge">🛡️ <strong>{{ data.clan.name }}</strong> &nbsp;·&nbsp; {{ data.role | replace('_', ' ') | title }}</div>
    {% else %}
      <div class="clan-badge">No Clan</div>
    {% endif %}
  </div>

  <h2>📈 Progression</h2>
  <div class="grid">
    <div class="stat-box">
      <div class="label">XP Level</div>
      <div class="value white">⭐ {{ data.expLevel }}</div>
    </div>
    <div class="stat-box">
      <div class="label">Current Trophies</div>
      <div class="value blue">🏆 {{ data.trophies }}</div>
    </div>
    <div class="stat-box">
      <div class="label">Best Trophies</div>
      <div class="value blue">🏅 {{ data.bestTrophies }}</div>
    </div>
    <div class="stat-box">
      <div class="label">Arena</div>
      <div class="value white" style="font-size:1rem; padding-top:4px;">{{ data.arena.name if data.arena else '—' }}</div>
    </div>
  </div>

  <h2>⚔️ Battle Stats</h2>
  <div class="grid">
    <div class="stat-box">
      <div class="label">Total Wins</div>
      <div class="value green">{{ data.wins }}</div>
    </div>
    <div class="stat-box">
      <div class="label">Losses</div>
      <div class="value red">{{ data.losses }}</div>
    </div>
    <div class="stat-box">
      <div class="label">3-Crown Wins</div>
      <div class="value green">👑 {{ data.threeCrownWins }}</div>
    </div>
    <div class="stat-box">
      <div class="label">Total Battles</div>
      <div class="value white">{{ data.battleCount }}</div>
    </div>
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
    <div class="stat-box">
      <div class="label">Donations (Season)</div>
      <div class="value white">{{ data.donations | default(0) }}</div>
    </div>
    <div class="stat-box">
      <div class="label">Donations Received</div>
      <div class="value white">{{ data.donationsReceived | default(0) }}</div>
    </div>
    <div class="stat-box">
      <div class="label">War Day Wins</div>
      <div class="value white">⚔️ {{ data.warDayWins | default(0) }}</div>
    </div>
    <div class="stat-box">
      <div class="label">3-Crown Win Rate</div>
      {% if data.wins > 0 %}
        <div class="value {% if (data.threeCrownWins / data.wins * 100) >= 30 %}green{% else %}white{% endif %}">
          {{ "%.1f" | format(data.threeCrownWins / data.wins * 100) }}%
        </div>
      {% else %}
        <div class="value white">—</div>
      {% endif %}
    </div>
    <div class="stat-box">
      <div class="label">Favourite Card</div>
      <div class="value white" style="font-size:1rem; padding-top:4px;">
        {{ data.currentFavouriteCard.name if data.currentFavouriteCard else '—' }}
      </div>
    </div>
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
  <div id="battles-section">
    <div class="no-battles">Loading recent battles...</div>
  </div>

  <script>
    async function loadPlayerBattles() {
      const tag = {{ data.tag | tojson }};
      const cleanTag = tag.replace('#', '');
      const section = document.getElementById('battles-section');
      try {
        const res = await fetch('/api/player/' + cleanTag + '/battles');
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const battles = await res.json();
        if (!Array.isArray(battles) || battles.length === 0) {
          section.innerHTML = '<div class="no-battles">No battles found. Run a harvest to populate battle history.</div>';
          return;
        }
        section.innerHTML = battles.map(function(b) {
          var cls = b.result || 'draw';
          var time = b.battle_time ? b.battle_time.replace('T', ' ').substring(0, 16) : '—';
          var oppName = b.opp_name || 'Unknown';
          var oppTag = b.opp_tag || '';
          var tc = b.team_crowns != null ? b.team_crowns : '—';
          var oc = b.opp_crowns != null ? b.opp_crowns : '—';
          var type = b.type || 'PvP';
          return '<div class="battle-row ' + cls + '">'
            + '<div>'
            + '<div class="battle-type">' + type + '</div>'
            + '<div class="battle-opp">vs ' + oppName + ' <span style="color:#555;font-size:0.8rem;">' + oppTag + '</span></div>'
            + '<div class="battle-time">' + time + '</div>'
            + '</div>'
            + '<div class="battle-score ' + cls + '">'
            + tc + ' – ' + oc
            + '<div style="font-size:0.8rem;font-weight:normal;">' + cls.toUpperCase() + '</div>'
            + '</div>'
            + '</div>';
        }).join('');
      } catch(e) {
        section.innerHTML = '<div class="no-battles">Error loading battles: ' + e.message + '</div>';
      }
    }
    document.addEventListener('DOMContentLoaded', loadPlayerBattles);
  </script>

</body>
</html>
"""
################################0000000000000000000000000000000000000000000000000000000000000000000000000000#####################################################################################################

DEFAULT_ADMIN_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Graveyard HQ</title>
<link href="https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Barlow+Condensed:wght@400;600;700&display=swap" rel="stylesheet">
<style>
  :root { --bg: #080a0f; --surface: #0d1117; --panel: #111820; --border: #1e2d3d; --accent: #00e5ff; --ok: #00e096; --warn: #ffaa00; --err: #ff3d71; --text: #c9d1d9; --dim: #4a5568; --font-mono: 'Share Tech Mono', monospace; --font-ui: 'Barlow Condensed', sans-serif; }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: var(--font-ui); min-height: 100vh; display: flex; flex-direction: column; }
  .shell { display: flex; flex: 1; height: 100vh; overflow: hidden; }
  .sidebar { width: 220px; background: var(--surface); border-right: 1px solid var(--border); padding: 20px 0; }
  .main { flex: 1; padding: 28px; overflow-y: auto; }
  .tab-pane { display: none; } .tab-pane.active { display: block; }
  .nav-btn { display: block; width: 100%; padding: 12px 20px; border: none; background: none; color: var(--dim); text-align: left; cursor: pointer; text-transform: uppercase; font-weight: bold; }
  .nav-btn.active { color: var(--accent); background: rgba(0,229,255,0.05); border-left: 3px solid var(--accent); }
  .war-table { width: 100%; border-collapse: collapse; font-family: var(--font-mono); font-size: 12px; }
  .war-table th, .war-table td { padding: 12px; border-bottom: 1px solid var(--border); text-align: left; }
  .btn-refresh { padding: 8px 16px; background: rgba(0,229,255,0.08); border: 1px solid var(--accent); color: var(--accent); cursor: pointer; border-radius: 4px; }
  .modal { display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.85); z-index: 1000; }
  .modal-content { background: var(--panel); margin: 10% auto; padding: 25px; width: 600px; border: 1px solid var(--border); border-radius: 8px; }
</style>
</head>
<body>
<div class="shell">
  <nav class="sidebar">
    <button class="nav-btn active" onclick="showTab('battles', this)">📜 Battle Logs</button>
    <button class="nav-btn" onclick="showTab('war', this)">⚔️ War Monitor</button>
    <button class="nav-btn" onclick="showTab('csv', this)">📄 CSV Export</button>
    <button class="nav-btn" onclick="showTab('admin', this)">⚙️ Admin Tools</button>
  </nav>
  <main class="main">
    <div class="tab-pane active" id="tab-battles">
      <div class="toolbar" style="display:flex; gap:10px; margin-bottom:20px;">
        <select id="battle-player-filter" class="form-select" onchange="loadBattles(1)">
            <option value="">All Players</option>
        </select>
        <button class="btn-refresh" onclick="loadBattles(1)">↻ Refresh Logs</button>
      </div>
      <div id="battle-stats-summary" style="margin-bottom:10px; color:var(--accent);"></div>
      <table class="war-table">
        <thead><tr><th>Time</th><th>Player</th><th>Result</th><th>Score</th><th>Deck (Click to View)</th></tr></thead>
        <tbody id="battles-body"></tbody>
      </table>
      <div id="pagination-controls" style="margin-top:20px; display:flex; gap:5px;"></div>
    </div>

    <div class="tab-pane" id="tab-war">
      <button class="btn-refresh" onclick="loadWar()">Refresh War Data</button>
      <div id="war-content" style="margin-top:20px;"></div>
    </div>

    <div class="tab-pane" id="tab-csv">
        <form id="csv-export-form" onsubmit="handleCustomCSVExport(event)">
          <label>1. Select Fields <input type="checkbox" onchange="toggleAllFields(this)"> Toggle All</label>
          <div id="csv-fields" style="display:grid; grid-template-columns:1fr 1fr 1fr; gap:10px; margin:10px 0;">
             <label><input type="checkbox" name="fields" value="name" checked> Name</label>
             <label><input type="checkbox" name="fields" value="tag" checked> Tag</label>
             <label><input type="checkbox" name="fields" value="trophies" checked> Trophies</label>
          </div>
          <button type="submit" class="btn-refresh">📥 Generate CSV</button>
        </form>
    </div>
  </main>
</div>

<div id="battle-modal" class="modal" onclick="this.style.display='none'">
  <div class="modal-content" onclick="event.stopPropagation()">
    <h3>Battle Details</h3>
    <div id="modal-body" style="display:flex; gap:20px; margin-top:15px;"></div>
  </div>
</div>

<script>
function showTab(name, btn) {
    document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
    document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
    document.getElementById('tab-' + name).classList.add('active');
    btn.classList.add('active');
}

async function loadBattles(page = 1) {
    const tag = document.getElementById('battle-player-filter').value;
    const res = await fetch(`/admin/api/battles?tag=${tag}&page=${page}`);
    const data = await res.json();
    
    document.getElementById('battles-body').innerHTML = data.battles.map(b => `
        <tr onclick='showBattleDetails(${JSON.stringify(b)})' style="cursor:pointer">
            <td>${b.battle_time.substring(0,16)}</td>
            <td>${b.player_name || b.player_tag}</td>
            <td class="${b.result}">${b.result.toUpperCase()}</td>
            <td>${b.team_crowns}-${b.opp_crowns}</td>
            <td>${(b.team_cards || []).length} cards used</td>
        </tr>
    `).join('');

    const pag = document.getElementById('pagination-controls');
    pag.innerHTML = Array.from({length: data.pages}, (_, i) => 
        `<button class="btn-refresh" onclick="loadBattles(${i+1})">${i+1}</button>`
    ).join('');
}

function showBattleDetails(b) {
    const body = document.getElementById('modal-body');
    const team = (b.team_cards || []).map(c => c.name).join(', ');
    const opp = (b.opponent_cards || []).map(c => c.name).join(', ');
    body.innerHTML = `<div style="flex:1"><h4>Your Deck</h4><p>${team}</p></div>
                      <div style="flex:1"><h4>Opponent Deck</h4><p>${opp}</p></div>`;
    document.getElementById('battle-modal').style.display = 'block';
}

function toggleAllFields(source) {
  document.querySelectorAll('#csv-export-form input[type="checkbox"]').forEach(cb => cb.checked = source.checked);
}

// Auto-populate filter from Roster cards
document.addEventListener('DOMContentLoaded', async () => {
    const res = await fetch('/');
    const text = await res.text();
    const parser = new DOMParser();
    const doc = parser.parseFromString(text, 'text/html');
    const filter = document.getElementById('battle-player-filter');
    doc.querySelectorAll('.player-card').forEach(card => {
        const name = card.querySelector('.p-name').textContent.trim();
        const tag = card.getAttribute('href').split('/').pop();
        filter.innerHTML += `<option value="${tag}">${name}</option>`;
    });
    loadBattles();
});
</script>
</body>
</html>"""