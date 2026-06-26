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
  
  .player-card { background: #161b22; border: 1px solid #1e2530; border-radius: 10px; padding: 14px 18px; margin-bottom: 10px; display: flex; align-items: center; justify-content: space-between; transition: border-color 0.2s, transform 0.15s; gap: 12px; flex-wrap: wrap; }
  .player-card:hover { border-color: #45a29e; transform: translateY(-1px); }
  .p-left { display: flex; flex-direction: column; gap: 3px; flex: 1; }
  .p-name { font-size: 1rem; font-weight: 700; color: #fff; text-decoration: none; }
  .p-name:hover { text-decoration: underline; color: #45a29e; }
  
  /* Role Styling */
  .role-leader { color: #f1c40f; font-weight: 800; font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.5px; }
  .role-coleader { color: #e67e22; font-weight: 700; font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.5px; }
  .role-elder { color: #3498db; font-size: 0.75rem; font-weight: 600; }
  .role-member { color: #6b7785; font-size: 0.75rem; }

  .p-right { display: flex; align-items: center; gap: 20px; }
  .p-stats-col { display: flex; flex-direction: column; align-items: flex-end; gap: 5px; }
  .p-trophies { font-size: 1rem; font-weight: 700; color: #f1c40f; }
  .p-stats-row { display: flex; gap: 12px; font-size: 0.75rem; color: #6b7785; }
  .p-stats-row span { color: #a0aab5; }
  
  .btn-action { background: #252d38; border: 1px solid #303a48; color: #fff; padding: 6px 12px; border-radius: 5px; font-size: 0.75rem; text-decoration: none; font-weight: bold; transition: background 0.2s; white-space: nowrap; }
  .btn-action:hover { background: #45a29e; border-color: #45a29e; color: #000; }

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
  <div class="hero-sub">{{ players | length }} members &middot; Click a name for analytics or log for battles</div>
</header>
<div class="container">
  <div class="main-col">
    {% if error %}<div class="error-banner">⚠️ {{ error }}</div>{% endif %}
    {% for p in players %}
    <div class="player-card">
      <div class="p-left">
        <a href="/player/{{ p.clean_tag }}" class="p-name cr-name">{{ p.name }}</a>
        <div class="role-{{ p.role | lower }}">{{ p.role | replace('coLeader', 'Co-Leader') | title }}</div>
      </div>
      <div class="p-right">
        <div class="p-stats-col">
          <div class="p-trophies">🏆 {{ p.trophies }}</div>
          <div class="p-stats-row">
            <div title="Fame">⭐ <span>{{ p.fame | default(0) }}</span></div>
            <div title="Win Streak">🔥 <span>{{ p.current_streak | default(0) }}</span></div>
            <div title="War Wins">⚔️ <span>{{ p.warDayWins | default(0) }}</span></div>
          </div>
        </div>
        <a href="/battles/{{ p.clean_tag }}" class="btn-action" title="View Battle Log">📜 Log</a>
      </div>
    </div>
    {% endfor %}
  </div>
  <div class="side-col">
    <h2>Hall of Fame</h2>
    <div class="hof-card" style="--hof-color: #f1c40f;">
      <div class="hof-label">Top Pusher</div>
      <div class="hof-name cr-name">{{ top_pusher.name if top_pusher else 'N/A' }}</div>
      <div class="hof-stat">🏆 {{ top_pusher.trophies if top_pusher else 0 }} Trophies</div>
    </div>
    <div class="hof-card" style="--hof-color: #2ecc71;">
      <div class="hof-label">Top Donator</div>
      <div class="hof-name cr-name">{{ top_donator.name if top_donator else 'N/A' }}</div>
      <div class="hof-stat">🎁 {{ top_donator.donations if top_donator else 0 }} Donations</div>
    </div>
    <div class="hof-card" style="--hof-color: #9b59b6;">
      <div class="hof-label">War Hero</div>
      <div class="hof-name cr-name">{{ war_hero.name if war_hero else 'N/A' }}</div>
      <div class="hof-stat">⚔️ {{ war_hero.warDayWins if war_hero else 0 }} War Wins</div>
    </div>
    <div class="hof-card" style="--hof-color: #e74c3c;">
      <div class="hof-label">On Fire</div>
      <div class="hof-name cr-name">{{ top_streak.name if top_streak else 'N/A' }}</div>
      <div class="hof-stat">🔥 {{ top_streak.current_streak if top_streak else 0 }} Win Streak</div>
    </div>
  </div>
</div>
</body>
</html>
"""

DEFAULT_PUBLIC_BATTLES_HTML = r"""
<!DOCTYPE html>
<html>
<head>
  <title>Battle Log for #{{ tag }}</title>
  <style>
    body { background: #0f0f0f; color: #eee; font-family: sans-serif; padding: 20px; max-width: 1000px; margin: 0 auto; }
    .nav { margin-bottom: 20px; }
    .nav a { color: #45a29e; text-decoration: none; font-weight: bold; background: #1a1a1a; padding: 8px 16px; border-radius: 5px; }
    .nav a:hover { background: #222; }
    .battle-row { background: #1a1a1a; padding: 20px; margin-bottom: 15px; border-radius: 8px; border-left: 5px solid #444; }
    .win { border-left-color: #2ecc71; }
    .loss { border-left-color: #e74c3c; }
    .battle-header { display: flex; justify-content: space-between; margin-bottom: 15px; align-items: flex-start; }
    .battle-title { font-size: 1.1rem; }
    .deck-container { display: flex; gap: 30px; flex-wrap: wrap; }
    .deck-side { display: flex; flex-direction: column; gap: 8px; }
    .deck-label { font-size: 0.8rem; color: #aaa; text-transform: uppercase; letter-spacing: 1px; }
    .deck-grid { display: flex; gap: 4px; }
    .deck-grid img { width: 40px; height: 48px; object-fit: contain; background: #222; border-radius: 4px; border: 1px solid #333; }
  </style>
</head>
<body>
  <div class="nav"><a href="/">← Back to Roster</a></div>
  <h1>Battle Log: #{{ tag }}</h1>
  {% for b in battles %}
    <div class="battle-row {{ b.result }}">
      <div class="battle-header">
        <div class="battle-title">
          <strong class="{{ b.result }}" style="color: {% if b.result == 'win' %}#2ecc71{% elif b.result == 'loss' %}#e74c3c{% else %}#f1c40f{% endif %}">{{ b.result | upper }}</strong> 
          vs {{ b.opp_name }} ({{ b.opp_clan | default('No Clan') }})
        </div>
        <div>
          <small style="color: #888;">{{ b.battle_time.replace('T', ' ').substring(0, 16) }}</small><br>
          <small style="color: #aaa;">Score: {{ b.team_crowns | default(0) }} - {{ b.opp_crowns | default(0) }}</small>
        </div>
      </div>
      <div class="deck-container">
        <div class="deck-side">
          <div class="deck-label">Team Deck</div>
          <div class="deck-grid">
            {% for card in b.team_cards %}
                <img src="{{ card.iconUrls.medium }}" title="{{ card.name }}">
            {% endfor %}
          </div>
        </div>
        <div class="deck-side">
          <div class="deck-label">Opponent Deck</div>
          <div class="deck-grid">
            {% for card in b.opponent_cards %}
                <img src="{{ card.iconUrls.medium }}" title="{{ card.name }}">
            {% endfor %}
          </div>
        </div>
      </div>
    </div>
  {% else %}
    <div style="background: #1a1a1a; padding: 20px; border-radius: 8px; text-align: center; color: #888;">No recent battles found.</div>
  {% endfor %}
</body>
</html>
"""

DEFAULT_PLAYER_HTML = r"""
<!DOCTYPE html>
<html>
<head>
  <title>{{ data.name }} - Analytics</title>
  <style>
    body { background: #0f0f0f; color: #eee; font-family: sans-serif; padding: 40px; max-width: 1000px; margin: 0 auto; }
    .nav { margin-bottom: 20px; }
    .nav a { color: #45a29e; text-decoration: none; font-weight: bold; background: #1a1a1a; padding: 8px 16px; border-radius: 5px; }
    .nav a:hover { background: #222; }
    
    .header-section { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; flex-wrap: wrap; gap: 20px; }
    
    .stats-header { display: flex; gap: 20px; background: #1a1a1a; padding: 20px; border-radius: 8px; flex: 1; min-width: 300px; justify-content: space-around; }
    .stat-box { text-align: center; display: flex; flex-direction: column; gap: 5px; }
    .stat-val { font-size: 1.5rem; font-weight: bold; color: #fff; }
    .stat-label { font-size: 0.8rem; color: #888; text-transform: uppercase; letter-spacing: 1px; }

    .deck-grid { display: grid; grid-template-columns: repeat(8, 1fr); gap: 10px; background: #1a1a1a; padding: 20px; border-radius: 8px; margin-bottom: 30px; }
    .card-box { text-align: center; font-size: 0.8rem; color: #aaa; }
    .card-box img { width: 100%; max-width: 70px; display: block; margin: 0 auto 5px; filter: drop-shadow(0 2px 4px rgba(0,0,0,0.5)); }
    
    .battle-row { background: #1a1a1a; padding: 15px; margin-bottom: 10px; border-radius: 8px; display: flex; flex-direction: column; gap: 12px; border-left: 4px solid #444; }
    .battle-row.win { border-left-color: #2ecc71; }
    .battle-row.loss { border-left-color: #e74c3c; }
    .battle-info { display: flex; justify-content: space-between; border-bottom: 1px solid #333; padding-bottom: 10px; }
    .side-decks { display: flex; gap: 30px; flex-wrap: wrap; }
    .deck-images { display: flex; gap: 3px; margin-top: 5px; }
    .deck-images img { width: 35px; height: 42px; object-fit: contain; background: #222; border-radius: 4px; border: 1px solid #333; }
  </style>
</head>
<body>
  <div class="nav"><a href="/">← Back to Roster</a></div>
  
  <div class="header-section">
    <div>
      <h1 style="margin: 0;">{{ data.name }}</h1>
      <small style="color: #888; font-size: 1.1rem;">{{ data.tag }}</small>
    </div>
    <a href="/battles/{{ data.tag | replace('#','') }}" style="background: #45a29e; color: white; padding: 12px 20px; border-radius: 5px; text-decoration: none; font-weight: bold; box-shadow: 0 4px 6px rgba(0,0,0,0.3);">📜 View Full Public Battle Log</a>
  </div>

  <div class="stats-header">
    <div class="stat-box"><span class="stat-val">🏆 {{ data.trophies | default(0) }}</span><span class="stat-label">Trophies</span></div>
    <div class="stat-box"><span class="stat-val">⭐ {{ data.fame | default(0) }}</span><span class="stat-label">War Fame</span></div>
    <div class="stat-box"><span class="stat-val">⚔️ {{ data.warDayWins | default(0) }}</span><span class="stat-label">War Wins</span></div>
    <div class="stat-box"><span class="stat-val">🔥 {{ data.current_streak | default(0) }}</span><span class="stat-label">Win Streak</span></div>
  </div>
  
  <h2>🃏 Current Battle Deck</h2>
  <div class="deck-grid">
    {% for card in data.currentDeck %}
      <div class="card-box">
        <img src="{{ card.iconUrls.medium }}" title="{{ card.name }}">
        <div>{{ card.name }}</div>
      </div>
    {% else %}
      <div style="grid-column: span 8; text-align: center; color: #888;">No current deck data available.</div>
    {% endfor %}
  </div>

  <h2>⚔️ Recent Battles</h2>
  <div id="battles-section"><div style="color: #888;">Loading battle data...</div></div>
  
  <script>
    async function loadPlayerBattles() {
      const tag = '{{ data.tag }}'.replace('#', '');
      try {
        const res = await fetch('/api/player/' + tag + '/battles');
        if (!res.ok) throw new Error('Failed to fetch');
        const battles = await res.json();
        
        if (battles.length === 0) {
            document.getElementById('battles-section').innerHTML = '<div style="background: #1a1a1a; padding: 20px; border-radius: 8px; color: #888; text-align: center;">No recent battles logged.</div>';
            return;
        }

        document.getElementById('battles-section').innerHTML = battles.map(b => {
          const resultColor = b.result === 'win' ? '#2ecc71' : b.result === 'loss' ? '#e74c3c' : '#f1c40f';
          const oppCards = b.opponent_cards || [];
          return `
            <div class="battle-row ${b.result}">
              <div class="battle-info">
                <div>
                  <strong style="color: ${resultColor}; text-transform: uppercase;">${b.result}</strong> vs ${b.opp_name || 'Unknown'} 
                  <span style="color: #666; font-size: 0.8rem; margin-left: 10px;">${b.type || 'Battle'}</span>
                </div>
                <div style="text-align: right;">
                  <small style="color: #aaa;">Score: ${b.team_crowns ?? '-'} - ${b.opp_crowns ?? '-'}</small><br>
                  <small style="color: #666;">${b.battle_time ? b.battle_time.replace('T', ' ').substring(0, 16) : ''}</small>
                </div>
              </div>
              <div class="side-decks">
                <div>
                  <small style="color: #888; text-transform: uppercase;">Your Deck</small>
                  <div class="deck-images">${(b.team_cards || []).map(c => `<img src="${c.iconUrls.medium}" title="${c.name}">`).join('')}</div>
                </div>
                <div>
                  <small style="color: #888; text-transform: uppercase;">Opponent Deck</small>
                  <div class="deck-images">${oppCards.map(c => `<img src="${c.iconUrls.medium}" title="${c.name}">`).join('')}</div>
                </div>
              </div>
            </div>
          `;
        }).join('');
      } catch (err) {
        document.getElementById('battles-section').innerHTML = '<div style="color: #e74c3c;">Failed to load battles.</div>';
      }
    }
    loadPlayerBattles();
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

DEFAULT_ADMIN_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Graveyard HQ | Admin</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Barlow+Condensed:wght@400;600;700&display=swap" rel="stylesheet">
<style>
  :root {
    --bg:        #080a0f;
    --surface:   #0d1117;
    --panel:     #111820;
    --border:    #1e2d3d;
    --accent:    #00e5ff;
    --ok:        #00e096;
    --warn:      #ffaa00;
    --err:       #ff3d71;
    --text:      #c9d1d9;
    --dim:       #4a5568;
    --font-mono: 'Share Tech Mono', monospace;
    --font-ui:   'Barlow Condensed', sans-serif;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: var(--font-ui); font-size: 15px; min-height: 100vh; display: flex; flex-direction: column; }

  /* ── Topbar ── */
  .topbar { display: flex; align-items: center; gap: 16px; padding: 12px 24px; background: var(--surface); border-bottom: 1px solid var(--border); position: sticky; top: 0; z-index: 100; }
  .topbar-title { font-size: 22px; font-weight: 700; letter-spacing: 2px; text-transform: uppercase; color: var(--accent); text-shadow: 0 0 18px rgba(0,229,255,0.35); flex: 1; }
  .topbar-title span { color: var(--dim); font-weight: 400; }
  .topbar-badge { font-family: var(--font-mono); font-size: 11px; padding: 3px 10px; border-radius: 3px; background: rgba(0,229,255,0.08); border: 1px solid var(--accent); color: var(--accent); letter-spacing: 1px; }
  .topbar a { color: var(--dim); text-decoration: none; font-size: 13px; letter-spacing: 1px; text-transform: uppercase; transition: color .2s; }
  .topbar a:hover { color: var(--text); }

  /* ── Shell ── */
  .shell { display: flex; flex: 1; height: calc(100vh - 53px); }
  .sidebar { width: 200px; background: var(--surface); border-right: 1px solid var(--border); padding: 20px 0; flex-shrink: 0; display: flex; flex-direction: column; gap: 2px; overflow-y: auto; }
  .nav-section { font-family: var(--font-mono); font-size: 10px; letter-spacing: 2px; color: var(--dim); padding: 14px 20px 6px; text-transform: uppercase; }
  .nav-btn { display: flex; align-items: center; gap: 10px; padding: 10px 20px; background: none; border: none; color: var(--dim); font-family: var(--font-ui); font-size: 14px; font-weight: 600; letter-spacing: .5px; text-transform: uppercase; cursor: pointer; text-align: left; width: 100%; border-left: 3px solid transparent; transition: all .15s; }
  .nav-btn:hover { color: var(--text); background: rgba(255,255,255,0.03); }
  .nav-btn.active { color: var(--accent); border-left-color: var(--accent); background: rgba(0,229,255,0.06); }
  .nav-icon { font-size: 16px; width: 20px; text-align: center; }
  .main { flex: 1; overflow-y: auto; padding: 28px 32px; }
  .tab-pane { display: none; }
  .tab-pane.active { display: block; }

  /* ── Page header ── */
  .page-header { display: flex; align-items: baseline; gap: 14px; margin-bottom: 24px; border-bottom: 1px solid var(--border); padding-bottom: 14px; }
  .page-title { font-size: 28px; font-weight: 700; letter-spacing: 1px; text-transform: uppercase; color: #fff; }
  .page-sub { font-family: var(--font-mono); font-size: 12px; color: var(--dim); letter-spacing: 1px; }

  /* ── Stat cards ── */
  .stat-row { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 14px; margin-bottom: 28px; }
  .stat-card { background: var(--panel); border: 1px solid var(--border); border-radius: 6px; padding: 16px 18px; position: relative; overflow: hidden; }
  .stat-card::before { content: ''; position: absolute; top: 0; left: 0; right: 0; height: 2px; background: var(--accent); opacity: .6; }
  .stat-card.ok::before   { background: var(--ok);   }
  .stat-card.warn::before { background: var(--warn); }
  .stat-card.err::before  { background: var(--err);  }
  .stat-label { font-family: var(--font-mono); font-size: 10px; letter-spacing: 2px; color: var(--dim); text-transform: uppercase; margin-bottom: 8px; }
  .stat-value { font-family: var(--font-mono); font-size: 26px; font-weight: 700; color: #fff; line-height: 1; }
  .stat-value.ok   { color: var(--ok);   }
  .stat-value.warn { color: var(--warn); }
  .stat-value.err  { color: var(--err);  }
  .stat-note { font-size: 11px; color: var(--dim); margin-top: 5px; }

  /* ── Diag cards ── */
  .diag-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(340px, 1fr)); gap: 16px; margin-bottom: 28px; }
  .diag-card { background: var(--panel); border: 1px solid var(--border); border-radius: 6px; overflow: hidden; }
  .diag-card-header { display: flex; align-items: center; justify-content: space-between; padding: 12px 16px; border-bottom: 1px solid var(--border); background: rgba(255,255,255,0.02); }
  .diag-card-title { font-size: 13px; font-weight: 700; letter-spacing: 1.5px; text-transform: uppercase; color: #fff; }
  .status-pill { font-family: var(--font-mono); font-size: 10px; padding: 3px 9px; border-radius: 20px; letter-spacing: 1px; text-transform: uppercase; font-weight: 700; }
  .pill-ok      { background: rgba(0,224,150,0.12);  color: var(--ok);   border: 1px solid rgba(0,224,150,0.3); }
  .pill-warn    { background: rgba(255,170,0,0.12);  color: var(--warn); border: 1px solid rgba(255,170,0,0.3); }
  .pill-err     { background: rgba(255,61,113,0.12);  color: var(--err);  border: 1px solid rgba(255,61,113,0.3); }
  .pill-loading { background: rgba(255,255,255,0.05); color: var(--dim);  border: 1px solid var(--border); }
  .diag-body { padding: 14px 16px; }
  .diag-row { display: flex; align-items: center; justify-content: space-between; padding: 6px 0; border-bottom: 1px solid rgba(255,255,255,0.04); font-family: var(--font-mono); font-size: 12px; }
  .diag-row:last-child { border-bottom: none; }
  .diag-key { color: var(--dim); }
  .diag-val { color: var(--text); text-align: right; }
  .diag-val.ok   { color: var(--ok);   }
  .diag-val.warn { color: var(--warn); }
  .diag-val.err  { color: var(--err);  }

  /* ── Section labels ── */
  .section-label { font-family: var(--font-mono); font-size: 10px; letter-spacing: 3px; text-transform: uppercase; color: var(--dim); margin-bottom: 12px; margin-top: 24px; display: flex; align-items: center; gap: 10px; }
  .section-label::after { content: ''; flex: 1; height: 1px; background: var(--border); }

  /* ── Buttons ── */
  .toolbar { display: flex; align-items: center; gap: 12px; margin-bottom: 24px; flex-wrap: wrap; }
  .btn-refresh { display: flex; align-items: center; gap: 8px; padding: 8px 18px; background: rgba(0,229,255,0.08); border: 1px solid var(--accent); border-radius: 4px; color: var(--accent); font-family: var(--font-ui); font-weight: 700; font-size: 13px; letter-spacing: 1px; text-transform: uppercase; cursor: pointer; transition: all .2s; text-decoration: none; }
  .btn-refresh:hover:not(:disabled) { background: rgba(0,229,255,0.16); }
  .btn-refresh:disabled { opacity: 0.45; cursor: not-allowed; }
  .btn-danger { display: flex; align-items: center; gap: 8px; padding: 8px 18px; background: rgba(255,61,113,0.08); border: 1px solid var(--err); border-radius: 4px; color: var(--err); font-family: var(--font-ui); font-weight: 700; font-size: 13px; letter-spacing: 1px; text-transform: uppercase; cursor: pointer; transition: all .2s; }
  .btn-danger:hover:not(:disabled) { background: rgba(255,61,113,0.16); }
  .btn-danger:disabled { opacity: 0.45; cursor: not-allowed; }
  .last-refresh { font-family: var(--font-mono); font-size: 11px; color: var(--dim); margin-left: auto; }

  /* ── Spinner ── */
  @keyframes spin { to { transform: rotate(360deg); } }
  .spin { display: inline-block; animation: spin .8s linear infinite; }

  /* ── War / battle table ── */
  .war-table { width: 100%; border-collapse: collapse; font-family: var(--font-mono); font-size: 12px; }
  .war-table th { text-align: left; font-size: 10px; letter-spacing: 2px; text-transform: uppercase; color: var(--dim); padding: 8px 12px; border-bottom: 1px solid var(--border); }
  .war-table td { padding: 9px 12px; border-bottom: 1px solid rgba(255,255,255,0.04); color: var(--text); vertical-align: middle; }
  .war-table tr:hover td { background: rgba(255,255,255,0.03); }

  /* ── Filter bar ── */
  .form-input, .form-select {
    padding: 8px 12px;
    background: #050709;
    color: #fff;
    border: 1px solid var(--border);
    border-radius: 4px;
    font-family: var(--font-mono);
    font-size: 12px;
    outline: none;
    transition: border-color .15s;
  }
  .form-input:focus, .form-select:focus { border-color: var(--accent); }
  .form-input::placeholder { color: var(--dim); }

  /* ── Result badges ── */
  .badge { display: inline-block; padding: 2px 8px; border-radius: 20px; font-size: 10px; font-weight: 700; letter-spacing: 1px; text-transform: uppercase; }
  .badge-win  { background: rgba(0,224,150,0.12); color: var(--ok);  border: 1px solid rgba(0,224,150,0.25); }
  .badge-loss { background: rgba(255,61,113,0.12); color: var(--err); border: 1px solid rgba(255,61,113,0.25); }
  .badge-draw { background: rgba(255,170,0,0.12);  color: var(--warn); border: 1px solid rgba(255,170,0,0.25); }

  /* ── Deck bar ── */
  .deck-bar { height: 5px; border-radius: 3px; background: var(--border); margin-top: 5px; overflow: hidden; max-width: 80px; }
  .deck-bar-fill { height: 100%; border-radius: 3px; background: var(--accent); transition: width .3s; }

  /* ── ═══════════════════════════════════════════ ── */
  /* ── UI EDITOR — improved                        ── */
  /* ── ═══════════════════════════════════════════ ── */

  .editor-shell {
    display: flex;
    flex-direction: column;
    gap: 0;
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 6px;
    overflow: hidden;
  }

  /* Toolbar strip across the top of the editor */
  .editor-topbar {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 10px 14px;
    background: rgba(255,255,255,0.02);
    border-bottom: 1px solid var(--border);
    flex-wrap: wrap;
  }
  .editor-topbar select.form-select { font-size: 12px; padding: 5px 10px; }

  .editor-btn {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 5px 13px;
    border-radius: 4px;
    font-family: var(--font-ui);
    font-weight: 700;
    font-size: 12px;
    letter-spacing: .8px;
    text-transform: uppercase;
    cursor: pointer;
    border: 1px solid;
    transition: background .15s, opacity .15s;
    white-space: nowrap;
  }
  .editor-btn:disabled { opacity: 0.4; cursor: not-allowed; }
  .editor-btn-load    { background: rgba(0,229,255,0.07);   border-color: var(--accent); color: var(--accent); }
  .editor-btn-load:hover:not(:disabled)    { background: rgba(0,229,255,0.16); }
  .editor-btn-deploy  { background: rgba(0,224,150,0.07);   border-color: var(--ok);     color: var(--ok);     }
  .editor-btn-deploy:hover:not(:disabled)  { background: rgba(0,224,150,0.16); }
  .editor-btn-preview { background: rgba(241,196,15,0.07);  border-color: #f1c40f;       color: #f1c40f;       }
  .editor-btn-preview:hover:not(:disabled) { background: rgba(241,196,15,0.16); }
  .editor-btn-reset   { background: rgba(255,61,113,0.07);  border-color: var(--err);    color: var(--err);    }
  .editor-btn-reset:hover:not(:disabled)   { background: rgba(255,61,113,0.16); }
  .editor-btn-diff    { background: rgba(255,170,0,0.07);   border-color: var(--warn);   color: var(--warn);   }
  .editor-btn-diff:hover:not(:disabled)    { background: rgba(255,170,0,0.16); }
  .editor-btn-copy    { background: rgba(255,255,255,0.04); border-color: var(--border); color: var(--text);   }
  .editor-btn-copy:hover:not(:disabled)    { background: rgba(255,255,255,0.09); }

  .editor-spacer { flex: 1; }

  /* Status strip below toolbar */
  .editor-statusbar {
    display: flex;
    align-items: center;
    gap: 16px;
    padding: 5px 14px;
    background: rgba(0,0,0,0.25);
    border-bottom: 1px solid var(--border);
    font-family: var(--font-mono);
    font-size: 10px;
    color: var(--dim);
    flex-wrap: wrap;
    min-height: 26px;
  }
  .editor-statusbar .sb-item { display: flex; align-items: center; gap: 5px; }
  .sb-dot { width: 6px; height: 6px; border-radius: 50%; background: var(--dim); flex-shrink: 0; }
  .sb-dot.loaded   { background: var(--ok); }
  .sb-dot.dirty    { background: var(--warn); }
  .sb-dot.empty    { background: var(--dim); }
  .sb-dot.deploying { background: var(--accent); animation: pulse 1s infinite; }
  @keyframes pulse { 0%,100% { opacity:1; } 50% { opacity:0.3; } }

  /* The actual textarea */
  .editor-textarea {
    width: 100%;
    padding: 16px 18px;
    background: #050709;
    color: var(--accent);
    font-family: var(--font-mono);
    font-size: 13px;
    border: none;
    line-height: 1.65;
    resize: vertical;
    outline: none;
    min-height: 480px;
    tab-size: 2;
  }
  .editor-textarea::selection { background: rgba(0,229,255,0.18); }

  /* Diff viewer */
  .diff-panel {
    display: none;
    background: #050709;
    border-top: 1px solid var(--border);
    padding: 14px 18px;
    font-family: var(--font-mono);
    font-size: 11px;
    max-height: 260px;
    overflow-y: auto;
    line-height: 1.7;
  }
  .diff-panel.open { display: block; }
  .diff-line-add { color: var(--ok);   white-space: pre-wrap; word-break: break-all; }
  .diff-line-del { color: var(--err);  white-space: pre-wrap; word-break: break-all; text-decoration: line-through; opacity: .7; }
  .diff-line-ctx { color: var(--dim);  white-space: pre-wrap; word-break: break-all; }
  .diff-empty { color: var(--dim); font-style: italic; }

  /* ── Log box ── */
  .log-box { background: #050709; border: 1px solid var(--border); border-radius: 6px; padding: 14px; font-family: var(--font-mono); font-size: 11px; color: var(--dim); max-height: 240px; overflow-y: auto; line-height: 1.7; white-space: pre-wrap; word-break: break-all; }
  .log-line-ok   { color: var(--ok);   }
  .log-line-warn { color: var(--warn); }
  .log-line-err  { color: var(--err);  }

  /* ── Battle modal ── */
  .modal { display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.85); z-index: 1000; align-items: center; justify-content: center; }
  .modal.open { display: flex; }
  .modal-content { background: var(--panel); width: 640px; max-width: 94vw; max-height: 82vh; border-radius: 8px; border: 1px solid var(--border); display: flex; flex-direction: column; overflow: hidden; }
  .modal-header { display: flex; align-items: center; justify-content: space-between; padding: 16px 20px; border-bottom: 1px solid var(--border); background: rgba(255,255,255,0.02); }
  .modal-header h3 { font-size: 14px; font-weight: 700; letter-spacing: 1px; text-transform: uppercase; color: #fff; }
  .modal-close { background: none; border: none; color: var(--dim); cursor: pointer; font-size: 18px; padding: 2px 6px; border-radius: 4px; transition: color .15s; }
  .modal-close:hover { color: #fff; }
  .modal-body { padding: 20px; overflow-y: auto; }
  .card-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; margin-top: 10px; }
  .card-chip { background: var(--surface); border: 1px solid var(--border); border-radius: 4px; padding: 6px 8px; font-size: 11px; font-family: var(--font-mono); text-align: center; color: var(--text); }
  .modal-meta { display: flex; gap: 24px; margin-bottom: 18px; font-family: var(--font-mono); font-size: 12px; flex-wrap: wrap; }
  .modal-meta-item span { color: var(--dim); margin-right: 4px; }
  .deck-section-title { font-family: var(--font-mono); font-size: 10px; letter-spacing: 2px; text-transform: uppercase; color: var(--dim); margin: 14px 0 8px; }

  /* ── Toasts ── */
  .toast-wrap { position: fixed; bottom: 24px; right: 24px; display: flex; flex-direction: column; gap: 8px; z-index: 9999; }
  .toast { padding: 10px 18px; border-radius: 5px; font-family: var(--font-mono); font-size: 12px; border: 1px solid; animation: fadeIn .25s ease; cursor: pointer; }
  @keyframes fadeIn { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: translateY(0); } }
  .toast-ok   { background: rgba(0,224,150,0.1);  border-color: var(--ok);     color: var(--ok);     }
  .toast-err  { background: rgba(255,61,113,0.1); border-color: var(--err);    color: var(--err);    }
  .toast-info { background: rgba(0,229,255,0.1);  border-color: var(--accent); color: var(--accent); }

  /* ── Scrollbars ── */
  ::-webkit-scrollbar { width: 6px; height: 6px; }
  ::-webkit-scrollbar-track { background: var(--bg); }
  ::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }
</style>
</head>
<body>

<header class="topbar">
  <div class="topbar-title">☠ Graveyard <span>HQ</span></div>
  <span class="topbar-badge">CLAN: #{{ clan_tag }}</span>
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
    <button class="nav-btn" onclick="showTab('users', this)"><span class="nav-icon">👤</span>User Access</button>
    <div class="nav-section">Danger Zone</div>
    <button class="nav-btn" onclick="showTab('admin', this)"><span class="nav-icon">⚙️</span>Admin Tools</button>
  </nav>

  <main class="main">

    <!-- ── DIAGNOSTICS ── -->
    <div class="tab-pane active" id="tab-diag">
      <div class="page-header">
        <div class="page-title">Diagnostics</div>
        <div class="page-sub" id="diag-env">Initializing…</div>
      </div>
      <div class="toolbar">
        <button class="btn-refresh" id="btn-diag-refresh" onclick="loadDiagnostics()">
          <span id="diag-spin">↻</span> Refresh
        </button>
        <span class="last-refresh" id="diag-last-refresh">Never refreshed</span>
      </div>
      <div class="stat-row">
        <div class="stat-card" id="sc-redis">   <div class="stat-label">Redis</div>       <div class="stat-value">—</div><div class="stat-note">Checking…</div></div>
        <div class="stat-card" id="sc-mongo">   <div class="stat-label">MongoDB</div>     <div class="stat-value">—</div><div class="stat-note">Checking…</div></div>
        <div class="stat-card" id="sc-crapi">   <div class="stat-label">CR API</div>      <div class="stat-value">—</div><div class="stat-note">Checking…</div></div>
        <div class="stat-card" id="sc-cache-keys"><div class="stat-label">Cache Keys</div><div class="stat-value">—</div><div class="stat-note">Redis key count</div></div>
        <div class="stat-card" id="sc-harvest"> <div class="stat-label">Last Harvest</div><div class="stat-value" style="font-size:15px">—</div><div class="stat-note">Snapshot timestamp</div></div>
      </div>
      <div class="section-label">Infrastructure</div>
      <div class="diag-grid">
        <div class="diag-card">
          <div class="diag-card-header"><div class="diag-card-title">⚡ Redis</div><span class="status-pill pill-loading" id="pill-redis">LOADING</span></div>
          <div class="diag-body" id="body-redis"></div>
        </div>
        <div class="diag-card">
          <div class="diag-card-header"><div class="diag-card-title">🍃 MongoDB</div><span class="status-pill pill-loading" id="pill-mongo">LOADING</span></div>
          <div class="diag-body" id="body-mongo"></div>
        </div>
        <div class="diag-card">
          <div class="diag-card-header"><div class="diag-card-title">🃏 CR API</div><span class="status-pill pill-loading" id="pill-crapi">LOADING</span></div>
          <div class="diag-body" id="body-crapi"></div>
        </div>
        <div class="diag-card">
          <div class="diag-card-header"><div class="diag-card-title">🤖 Bot Process</div><span class="status-pill pill-loading" id="pill-bot">LOADING</span></div>
          <div class="diag-body" id="body-bot"></div>
        </div>
      </div>
      <div class="section-label">Cache &amp; Data</div>
      <div class="diag-grid">
        <div class="diag-card">
          <div class="diag-card-header"><div class="diag-card-title">📊 Cache Stats</div></div>
          <div class="diag-body" id="body-cache"></div>
        </div>
        <div class="diag-card">
          <div class="diag-card-header"><div class="diag-card-title">⏳ Tasks</div></div>
          <div class="diag-body" id="body-tasks"></div>
        </div>
      </div>
      <div class="section-label">Event Log</div>
      <div class="log-box" id="diag-log">Waiting for data…</div>
    </div>

    <!-- ── WAR MONITOR ── -->
    <div class="tab-pane" id="tab-war">
      <div class="page-header">
        <div class="page-title">War Monitor</div>
        <div class="page-sub">Current River Race</div>
      </div>
      <div class="toolbar">
        <button class="btn-refresh" onclick="loadWar()">↻ Refresh</button>
        <span class="last-refresh" id="war-last-refresh"></span>
      </div>
      <div id="war-content">
        <div style="color:var(--dim); font-family:var(--font-mono); font-size:12px;">Click refresh to load war data.</div>
      </div>
    </div>

    <!-- ── BATTLE LOGS ── -->
    <div class="tab-pane" id="tab-battles">
      <div class="page-header">
        <div class="page-title">Battle Logs</div>
        <div class="page-sub">Raw Combat Feed from MongoDB</div>
      </div>
      <div class="toolbar">
        <input class="form-input" id="battle-filter" placeholder="Filter by player or tag…" oninput="filterBattles()" style="width:200px">
        <select class="form-select" id="result-filter" onchange="filterBattles()">
          <option value="">All results</option>
          <option value="win">Wins only</option>
          <option value="loss">Losses only</option>
          <option value="draw">Draws only</option>
        </select>
        <button class="btn-refresh" onclick="loadBattles()">↻ Fetch Latest</button>
        <span class="last-refresh" id="battles-last-refresh"></span>
      </div>
      <div id="battles-status"></div>
      <div class="diag-card" style="padding:0; overflow-x:auto;">
        <table class="war-table">
          <thead>
            <tr>
              <th>Time (UTC)</th>
              <th>Player</th>
              <th>Tag</th>
              <th>Type</th>
              <th>Result</th>
              <th>Score</th>
              <th>Opponent</th>
              <th>Decks</th>
            </tr>
          </thead>
          <tbody id="battles-body">
            <tr><td colspan="8" style="text-align:center; padding:24px; color:var(--dim);">Click fetch to load the latest 100 records.</td></tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- ── HARVEST LOG ── -->
    <div class="tab-pane" id="tab-harvest">
      <div class="page-header">
        <div class="page-title">Harvest Log</div>
        <div class="page-sub">Historical Data &amp; Manual Triggers</div>
      </div>
      <div class="toolbar">
        <button class="btn-danger" onclick="triggerManualHarvest()" style="background:rgba(241,196,15,0.1); border-color:#f1c40f; color:#f1c40f;">
          ⚡ Force Manual Snapshot
        </button>
        <button class="btn-refresh" onclick="loadDiagnostics()">↻ Refresh Status</button>
      </div>
      <div class="diag-grid">
        <div class="diag-card">
          <div class="diag-card-header"><div class="diag-card-title">📡 Current Harvest Info</div></div>
          <div class="diag-body" id="harvest-detail-body">Load diagnostics first.</div>
        </div>
        <div class="diag-card">
          <div class="diag-card-header"><div class="diag-card-title">📅 Snapshot History</div></div>
          <div class="diag-body" id="harvest-history-body" style="max-height:200px; overflow-y:auto;">Load diagnostics first.</div>
        </div>
      </div>
    </div>

    <!-- ── CSV EXPORT ── -->
    <div class="tab-pane" id="tab-csv">
      <div class="page-header">
        <div class="page-title">Data Exporter</div>
        <div class="page-sub">Generate Custom CSV &amp; Computed Logic</div>
      </div>
      <div class="diag-card" style="padding:24px;">
        <label style="color:var(--dim); font-family:var(--font-mono); font-size:12px; text-transform:uppercase;">1. Select Fields</label>
        <div style="display:grid; grid-template-columns:repeat(auto-fit, minmax(150px,1fr)); gap:10px; margin:12px 0 24px; color:#fff; font-family:var(--font-mono); font-size:13px;">
          <label><input type="checkbox" name="csv-fields" value="name" checked> Name</label>
          <label><input type="checkbox" name="csv-fields" value="tag" checked> Tag</label>
          <label><input type="checkbox" name="csv-fields" value="role" checked> Role</label>
          <label><input type="checkbox" name="csv-fields" value="trophies" checked> Trophies</label>
          <label><input type="checkbox" name="csv-fields" value="fame" checked> War Fame</label>
          <label><input type="checkbox" name="csv-fields" value="totalWins"> Total Wins</label>
          <label><input type="checkbox" name="csv-fields" value="totalLosses"> Total Losses</label>
          <label><input type="checkbox" name="csv-fields" value="current_streak"> Win Streak</label>
          <label><input type="checkbox" name="csv-fields" value="donations"> Donations</label>
          <label><input type="checkbox" name="csv-fields" value="warDayWins"> War Day Wins</label>
          <label><input type="checkbox" name="csv-fields" value="decksUsedToday" checked> Decks Used</label>
          <label><input type="checkbox" name="csv-fields" value="decksRemaining" checked> Decks Remaining</label>
        </div>
        <label style="color:var(--dim); font-family:var(--font-mono); font-size:12px; text-transform:uppercase;">2. Computed Formulas</label>
        <div style="display:grid; gap:10px; margin:12px 0 24px; color:#fff; font-family:var(--font-mono); font-size:13px;">
          <label>
            <input type="checkbox" id="formula-winrate">
            <strong>Win Rate %</strong>
            <span style="color:var(--dim);"> ( totalWins / (totalWins + totalLosses) * 100 )</span>
          </label>
          <label>
            <input type="checkbox" id="formula-warpart">
            <strong>War Participation %</strong>
            <span style="color:var(--dim);"> ( decksUsedToday / (decksUsedToday + decksRemaining) * 100 )</span>
          </label>
        </div>
        <button class="btn-refresh" onclick="handleCustomCSVExport()" style="border-color:var(--ok); color:var(--ok); background:rgba(0,224,150,0.08);">
          📥 Generate &amp; Download CSV
        </button>
      </div>
    </div>

    <!-- ── UI EDITOR ── -->
    <div class="tab-pane" id="tab-editor">
      <div class="page-header">
        <div class="page-title">UI Editor</div>
        <div class="page-sub">Edit, preview and deploy HTML templates</div>
      </div>

      <div class="editor-shell">

        <!-- Top toolbar -->
        <div class="editor-topbar">
          <select id="editor-template-name" class="form-select" onchange="onTemplateChange()">
            <option value="roster">Roster (Home)</option>
            <option value="player">Player Profile</option>
            <option value="admin">Admin Dashboard</option>
            <option value="link">Discord Link Page</option>
          </select>

          <button class="editor-btn editor-btn-load" onclick="fetchTemplateForEditor('current')" id="btn-load-live">
            ↓ Load Live
          </button>
          <button class="editor-btn editor-btn-load" onclick="fetchTemplateForEditor('default')" id="btn-load-default">
            ↓ Load Default
          </button>

          <div class="editor-spacer"></div>

          <button class="editor-btn editor-btn-copy" onclick="copyEditorToClipboard()" id="btn-copy">
            ⎘ Copy
          </button>
          <button class="editor-btn editor-btn-diff" onclick="toggleDiff()" id="btn-diff" disabled>
            ± Diff
          </button>
          <button class="editor-btn editor-btn-preview" onclick="previewTemplate()" id="btn-preview" disabled>
            👁 Preview
          </button>
          <button class="editor-btn editor-btn-deploy" onclick="deployTemplate()" id="btn-deploy" disabled>
            🚀 Deploy
          </button>
          <button class="editor-btn editor-btn-reset" onclick="resetTemplate()" id="btn-reset" disabled>
            ↩ Reset
          </button>
        </div>

        <!-- Status bar -->
        <div class="editor-statusbar">
          <div class="sb-item"><div class="sb-dot" id="sb-dot"></div><span id="sb-state">No template loaded</span></div>
          <div class="sb-item" id="sb-template" style="display:none">Template: <strong id="sb-tpl-name">—</strong></div>
          <div class="sb-item" id="sb-source-item" style="display:none">Source: <span id="sb-source">—</span></div>
          <div class="sb-item" id="sb-chars-item" style="display:none"><span id="sb-chars">0</span> chars</div>
          <div class="sb-item" id="sb-lines-item" style="display:none"><span id="sb-lines">0</span> lines</div>
          <div class="sb-item" id="sb-time-item" style="display:none">Loaded <span id="sb-load-time">—</span></div>
        </div>

        <!-- Textarea -->
        <textarea
          id="editor-html-content"
          class="editor-textarea"
          spellcheck="false"
          placeholder="Load a template above to start editing…"
        ></textarea>

        <!-- Diff panel (hidden by default) -->
        <div class="diff-panel" id="diff-panel">
          <div id="diff-content"><span class="diff-empty">Load a template and make changes to see a diff.</span></div>
        </div>

      </div><!-- /editor-shell -->
    </div>
    
    <!-- ── USER ACCESS ── -->
    <div class="tab-pane" id="tab-users">
      <div class="page-header">
        <div class="page-title">User Access</div>
        <div class="page-sub">Manage Site Administration Privileges</div>
      </div>
      <form method="POST" action="/admin/users/update" class="diag-card" style="padding:20px; max-width: 400px;">
        <div style="margin-bottom: 15px;">
            <label style="color:var(--dim); font-family:var(--font-mono); font-size:12px; text-transform:uppercase;">Discord User ID</label>
            <input type="text" name="discord_id" placeholder="e.g. 123456789012345678" class="form-input" style="width: 100%; margin-top: 5px;" required>
        </div>
        <div style="margin-bottom: 15px;">
            <label style="color:var(--dim); font-family:var(--font-mono); font-size:12px; text-transform:uppercase;">Status</label>
            <select name="status" class="form-select" style="width: 100%; margin-top: 5px;">
                <option value="admin">Promote to Admin</option>
                <option value="member">Demote to Member</option>
            </select>
        </div>
        <button type="submit" class="btn-refresh" style="width: 100%; justify-content: center;">Update Status</button>
      </form>
    </div>

    <!-- ── ADMIN TOOLS ── -->
    <div class="tab-pane" id="tab-admin">
      <div class="page-header">
        <div class="page-title">Admin Tools</div>
        <div class="page-sub">Careful in here</div>
      </div>
      <div class="diag-grid">
        <div class="diag-card">
          <div class="diag-card-header"><div class="diag-card-title">🔄 Cache Flush</div></div>
          <div class="diag-body" style="display:flex; flex-direction:column; gap:10px;">
            <p style="font-size:12px; color:var(--dim); font-family:var(--font-mono);">Flush all Redis CR API cache keys. Use when data looks stale.</p>
            <button class="btn-danger" onclick="confirmFlushCache()">⚠ Flush CR Cache</button>
          </div>
        </div>
        <div class="diag-card">
          <div class="diag-card-header"><div class="diag-card-title">🩺 Health Check API</div></div>
          <div class="diag-body" style="display:flex; flex-direction:column; gap:10px;">
            <p style="font-size:12px; color:var(--dim); font-family:var(--font-mono);">Raw JSON payload of all internal diagnostics.</p>
            <button class="btn-refresh" onclick="window.open('/admin/diagnostics','_blank')">Open Raw JSON ↗</button>
          </div>
        </div>
        <div class="diag-card">
          <div class="diag-card-header"><div class="diag-card-title">⚡ Manual Harvest</div></div>
          <div class="diag-body" style="display:flex; flex-direction:column; gap:10px;">
            <p style="font-size:12px; color:var(--dim); font-family:var(--font-mono);">Force a snapshot outside the scheduled window.</p>
            <button class="btn-danger" onclick="triggerManualHarvest()" style="background:rgba(241,196,15,0.1); border-color:#f1c40f; color:#f1c40f;">⚡ Trigger Now</button>
          </div>
        </div>
      </div>
    </div>

  </main>
</div>

<!-- Battle detail modal -->
<div id="battle-modal" class="modal" onclick="closeModal()">
  <div class="modal-content" onclick="event.stopPropagation()">
    <div class="modal-header">
      <h3>Battle Details</h3>
      <button class="modal-close" onclick="closeModal()">✕</button>
    </div>
    <div class="modal-body" id="modal-body"></div>
  </div>
</div>

<div class="toast-wrap" id="toast-wrap"></div>

<script>
// ── State ─────────────────────────────────────────────────────────────────────
var allBattles     = [];
var filteredBattles = [];   
var _logLines      = [];

// Editor state
var editorOriginal   = '';   
var editorDirty      = false;
var editorDeploying  = false;
var editorTemplateName = '';

// ── Navigation ────────────────────────────────────────────────────────────────
function showTab(name, btn) {
  document.querySelectorAll('.tab-pane').forEach(function(p) { p.classList.remove('active'); });
  document.querySelectorAll('.nav-btn').forEach(function(b)  { b.classList.remove('active'); });
  var pane = document.getElementById('tab-' + name);
  if (pane) pane.classList.add('active');
  if (btn)  btn.classList.add('active');
}

// ── Utilities ─────────────────────────────────────────────────────────────────
function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}

function toast(msg, type, duration) {
  type     = type     || 'info';
  duration = duration || 3500;
  var wrap = document.getElementById('toast-wrap');
  var t = document.createElement('div');
  t.className   = 'toast toast-' + type;
  t.textContent = msg;
  t.onclick = function() { t.remove(); };
  wrap.appendChild(t);
  setTimeout(function() { if (t.parentNode) t.remove(); }, duration);
}

function appendLog(msg, level) {
  level = level || 'info';
  var ts = new Date().toLocaleTimeString();
  _logLines.push({ ts: ts, msg: msg, level: level });
  if (_logLines.length > 200) _logLines.shift();
  var box = document.getElementById('diag-log');
  if (!box) return;
  box.innerHTML = _logLines.map(function(l) {
    return '<span class="log-line-' + l.level + '">[' + l.ts + '] ' + esc(l.msg) + '</span>';
  }).join('\n');
  box.scrollTop = box.scrollHeight;
}

function setPill(id, text, type) {
  var el = document.getElementById(id);
  if (el) { el.className = 'status-pill pill-' + type; el.textContent = text; }
}

function setStatCard(id, value, note, status) {
  var card = document.getElementById(id);
  if (!card) return;
  card.className = 'stat-card ' + status;
  card.querySelector('.stat-value').className = 'stat-value ' + status;
  card.querySelector('.stat-value').textContent = value;
  card.querySelector('.stat-note').textContent  = note;
}

function renderRows(rows) {
  return rows.map(function(r) {
    return '<div class="diag-row"><span class="diag-key">' + esc(r[0]) + '</span>'
      + '<span class="diag-val ' + (r[2] || '') + '">' + esc(String(r[1])) + '</span></div>';
  }).join('');
}

function formatBattleTime(raw) {
  if (!raw) return '—';
  var m = String(raw).match(/^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})/);
  if (m) return m[1]+'-'+m[2]+'-'+m[3]+' '+m[4]+':'+m[5];
  return raw.substring(0, 16);
}

// ── Diagnostics ───────────────────────────────────────────────────────────────
async function loadDiagnostics() {
  var btn  = document.getElementById('btn-diag-refresh');
  var spin = document.getElementById('diag-spin');
  btn.disabled = true;
  spin.className = 'spin';
  appendLog('Fetching /admin/diagnostics…');
  try {
    var resp = await fetch('/admin/diagnostics');
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    var d = await resp.json();
    appendLog('Response received (HTTP 200)', 'ok');
    renderDiagnostics(d);
    document.getElementById('diag-last-refresh').textContent = 'Last refresh: ' + new Date().toLocaleTimeString();
  } catch(e) {
    appendLog('Error: ' + e.message, 'err');
    toast('Failed to load diagnostics: ' + e.message, 'err');
  } finally {
    btn.disabled = false;
    spin.className = '';
  }
}

function renderDiagnostics(d) {
  document.getElementById('diag-env').textContent =
    'v' + (d.version || '?') + ' · ' + (d.environment || 'unknown') + ' · ' + (d.hostname || '');

  var redis = d.redis || {};
  var redisOk = redis.status === 'ok';
  setPill('pill-redis', redisOk ? 'ONLINE' : 'OFFLINE', redisOk ? 'ok' : 'err');
  setStatCard('sc-redis', redisOk ? '✓' : '✗', redis.ping_ms != null ? redis.ping_ms + 'ms ping' : 'unreachable', redisOk ? 'ok' : 'err');
  document.getElementById('body-redis').innerHTML = renderRows([
    ['Status',     redis.status     || 'unknown', redisOk ? 'ok' : 'err'],
    ['Ping',       redis.ping_ms    != null ? redis.ping_ms + ' ms' : 'N/A'],
    ['Used Memory',redis.used_memory || 'N/A'],
    ['Total Keys', redis.total_keys  != null ? redis.total_keys : 'N/A']
  ]);

  var mongo = d.mongo || {};
  var mongoOk = mongo.status === 'ok';
  setPill('pill-mongo', mongoOk ? 'ONLINE' : 'OFFLINE', mongoOk ? 'ok' : 'err');
  setStatCard('sc-mongo', mongoOk ? '✓' : '✗', mongo.ping_ms != null ? mongo.ping_ms + 'ms ping' : 'unreachable', mongoOk ? 'ok' : 'err');
  document.getElementById('body-mongo').innerHTML = renderRows([
    ['Status',      mongo.status      || 'unknown', mongoOk ? 'ok' : 'err'],
    ['Ping',        mongo.ping_ms     != null ? mongo.ping_ms + ' ms' : 'N/A'],
    ['Snapshots',   mongo.snapshot_count != null ? mongo.snapshot_count : 'N/A'],
    ['Battle Docs', mongo.battle_count   != null ? mongo.battle_count   : 'N/A']
  ]);

  var api = d.cr_api || {};
  var apiOk  = api.status === 'ok';
  var apiCls = apiOk ? 'ok' : (api.status === 'rate_limited' ? 'warn' : 'err');
  setPill('pill-crapi', (api.status || 'unknown').toUpperCase(), apiCls);
  setStatCard('sc-crapi', api.status_code || '—', api.latency_ms != null ? api.latency_ms + 'ms' : 'unreachable', apiCls);
  document.getElementById('body-crapi').innerHTML = renderRows([
    ['Status',          api.status          || 'unknown', apiCls],
    ['HTTP Code',       api.status_code     != null ? api.status_code  : 'N/A'],
    ['Latency',         api.latency_ms      != null ? api.latency_ms + ' ms' : 'N/A'],
    ['Endpoint Tested', api.endpoint_tested || 'N/A']
  ]);

  var bot = d.bot || {};
  var botOk = bot.connected === true;
  setPill('pill-bot', botOk ? 'CONNECTED' : 'OFFLINE', botOk ? 'ok' : 'err');
  document.getElementById('body-bot').innerHTML = renderRows([
    ['Discord WS', bot.connected ? 'Connected' : 'Disconnected', bot.connected ? 'ok' : 'err'],
    ['Latency',    bot.latency_ms != null ? bot.latency_ms + ' ms' : 'N/A'],
    ['Uptime',     bot.uptime     || 'N/A']
  ]);

  var cache = d.cache || {};
  var totalKeys = cache.total_keys != null ? cache.total_keys : 0;
  setStatCard('sc-cache-keys', totalKeys, 'keys in store', totalKeys > 0 ? 'ok' : 'warn');
  document.getElementById('body-cache').innerHTML = renderRows([
    ['Backend',            cache.backend               || 'unknown'],
    ['Total Keys',         cache.total_keys            != null ? cache.total_keys            : 0],
    ['HTML Cache Entries', cache.html_cache_entries    != null ? cache.html_cache_entries    : 0]
  ]);

  var harv = d.harvest || {};
  var harvOk = !!harv.last_run;
  setStatCard('sc-harvest', harv.last_run || 'Never', harv.snapshots_saved ? harv.snapshots_saved + ' snaps' : 'no data', harvOk ? 'ok' : 'warn');
  document.getElementById('harvest-detail-body').innerHTML = renderRows([
    ['Last Run',           harv.last_run               || 'Never'],
    ['Status',             harv.status                 || 'unknown', harv.status === 'ok' ? 'ok' : 'warn'],
    ['Snapshots Saved',    harv.snapshots_saved        != null ? harv.snapshots_saved        : 'N/A'],
    ['Profiles Saved',     harv.profiles_saved         != null ? harv.profiles_saved         : 'N/A'],
    ['Battles Saved',      harv.battles_saved          != null ? harv.battles_saved          : 'N/A'],
    ['Duration',           harv.duration_s             != null ? harv.duration_s + 's'       : 'N/A'],
    ['Members',            harv.member_count           != null ? harv.member_count           : 'N/A'],
    ['War Participants',   harv.war_participants_found != null ? harv.war_participants_found  : 'N/A']
  ]);

  var historyList = (harv.history_dates || []).map(function(date) {
    return '<div class="diag-row" style="padding:10px 0;">'
      + '<span class="diag-key" style="color:#fff;">' + esc(date) + '</span>'
      + '<span class="diag-val">'
      + '<button class="btn-refresh" style="padding:4px 10px; font-size:11px;" '
      + 'onclick="window.open(\'/admin/api/snapshot/' + esc(date) + '\',\'_blank\')">👀 View</button>'
      + '</span></div>';
  }).join('');
  document.getElementById('harvest-history-body').innerHTML = historyList || 'No snapshots found.';

  var tasks = d.tasks || {};
  document.getElementById('body-tasks').innerHTML = renderRows([
    ['Snapshot Loop', tasks.snapshot_loop || 'unknown'],
    ['Next Snapshot', tasks.next_snapshot || 'N/A']
  ]);

  appendLog('Diagnostics render complete.', 'ok');
}

// ── War Monitor ───────────────────────────────────────────────────────────────
async function loadWar() {
  var content = document.getElementById('war-content');
  content.innerHTML = '<div style="color:var(--dim);font-family:var(--font-mono);font-size:12px;"><span class="spin">↻</span> Fetching live war data…</div>';
  try {
    var res  = await fetch('/admin/api/war');
    if (!res.ok) throw new Error('HTTP ' + res.status);
    var data = await res.json();
    if (data.error) throw new Error(data.error);

    var participants = (data.clan && data.clan.participants) ? data.clan.participants : [];
    if (!participants.length) throw new Error('No participants found.');

    var totalFame  = participants.reduce(function(s,p){ return s + (p.fame||0); }, 0);
    var totalDecks = participants.reduce(function(s,p){ return s + (p.decksUsedToday||0); }, 0);
    var maxDecks   = participants.length * 4;
    var sorted     = participants.slice().sort(function(a,b){ return (b.fame||0)-(a.fame||0); });

    content.innerHTML =
      '<div class="stat-row">'
      + '<div class="stat-card ok"><div class="stat-label">Race State</div><div class="stat-value" style="font-size:18px;">' + esc((data.state||'unknown').toUpperCase()) + '</div></div>'
      + '<div class="stat-card ok"><div class="stat-label">Clan Fame</div><div class="stat-value">⭐ ' + esc(totalFame) + '</div></div>'
      + '<div class="stat-card ok"><div class="stat-label">Deck Usage</div><div class="stat-value">' + esc(totalDecks) + ' / ' + esc(maxDecks) + '</div></div>'
      + '<div class="stat-card ok"><div class="stat-label">Participants</div><div class="stat-value">👥 ' + esc(participants.length) + '</div></div>'
      + '</div>'
      + '<div class="diag-card" style="overflow-x:auto;">'
      + '<table class="war-table"><thead><tr>'
      + '<th>Player</th><th>Tag</th><th>Fame</th><th>Decks Today</th><th>War Wins</th>'
      + '</tr></thead><tbody>'
      + sorted.map(function(p) {
          var decks = p.decksUsedToday || 0;
          var pct   = Math.round(decks / 4 * 100);
          return '<tr>'
            + '<td><strong>' + esc(p.name) + '</strong></td>'
            + '<td style="color:var(--dim)">' + esc(p.tag) + '</td>'
            + '<td style="color:var(--accent); font-weight:700;">' + esc(p.fame||0) + '</td>'
            + '<td>' + esc(decks) + '/4'
            + '<div class="deck-bar"><div class="deck-bar-fill" style="width:' + pct + '%"></div></div></td>'
            + '<td>' + esc(p.warDayWins||0) + '</td>'
            + '</tr>';
        }).join('')
      + '</tbody></table></div>';

    document.getElementById('war-last-refresh').textContent = 'Last refresh: ' + new Date().toLocaleTimeString();
    toast('War data loaded (' + participants.length + ' participants).', 'ok');
  } catch(err) {
    content.innerHTML = '<div style="color:var(--err);font-family:var(--font-mono);">Error: ' + esc(err.message) + '</div>';
    toast('Failed to load war data: ' + err.message, 'err');
  }
}

// ── Battle Logs ───────────────────────────────────────────────────────────────
async function loadBattles() {
  var tbody  = document.getElementById('battles-body');
  var status = document.getElementById('battles-status');
  tbody.innerHTML = '<tr><td colspan="8" style="text-align:center; padding:24px; color:var(--dim);"><span class="spin">↻</span> Loading…</td></tr>';
  status.innerHTML = '';
  try {
    var res  = await fetch('/admin/api/battles');
    if (!res.ok) throw new Error('HTTP ' + res.status);
    var data = await res.json();
    allBattles = Array.isArray(data) ? data : (data.battles || []);
    filteredBattles = allBattles.slice(); 
    document.getElementById('battles-last-refresh').textContent = 'Last refresh: ' + new Date().toLocaleTimeString();
    renderBattles(filteredBattles);
    toast('Loaded ' + allBattles.length + ' battle records.', 'ok');
  } catch(e) {
    tbody.innerHTML = '<tr><td colspan="8" style="text-align:center; padding:24px; color:var(--err);">Error: ' + esc(e.message) + '</td></tr>';
    toast('Failed to load battles: ' + e.message, 'err');
  }
}

function renderBattles(battles) {
  filteredBattles = battles;
  var tbody = document.getElementById('battles-body');
  if (!battles.length) {
    tbody.innerHTML = '<tr><td colspan="8" style="text-align:center; padding:24px; color:var(--dim);">No records match your filter.</td></tr>';
    return;
  }
  tbody.innerHTML = battles.map(function(b, i) {
    var result = b.result || '';
    var badgeCls = result === 'win' ? 'badge-win' : (result === 'loss' ? 'badge-loss' : 'badge-draw');
    var resultBadge = result
      ? '<span class="badge ' + badgeCls + '">' + esc(result) + '</span>'
      : '—';
    var decks = (b.team_cards || []).length;
    return '<tr onclick="showBattleDetails(' + i + ')" style="cursor:pointer">'
      + '<td style="color:var(--dim)">' + esc(formatBattleTime(b.battle_time)) + '</td>'
      + '<td><strong>' + esc(b.player_name || '—') + '</strong></td>'
      + '<td style="color:var(--dim)">' + esc(b.player_tag || '—') + '</td>'
      + '<td style="color:var(--dim)">' + esc(b.type || '—') + '</td>'
      + '<td>' + resultBadge + '</td>'
      + '<td>' + (b.team_crowns != null ? b.team_crowns : '—') + ' – ' + (b.opp_crowns != null ? b.opp_crowns : '—') + '</td>'
      + '<td>' + esc(b.opp_name || '—') + '</td>'
      + '<td style="color:var(--dim)">' + decks + ' cards</td>'
      + '</tr>';
  }).join('');
}

function filterBattles() {
  var text   = document.getElementById('battle-filter').value.toUpperCase();
  var result = document.getElementById('result-filter').value;
  var filtered = allBattles.filter(function(b) {
    var matchText = !text
      || (b.player_name || '').toUpperCase().includes(text)
      || (b.player_tag  || '').toUpperCase().includes(text);
    var matchResult = !result || b.result === result;
    return matchText && matchResult;
  });
  renderBattles(filtered);
}

function showBattleDetails(i) {
  var b = filteredBattles[i];
  if (!b) return;
  var teamCards = b.team_cards || [];
  var oppCards  = b.opponent_cards || [];

  function cardGrid(cards) {
    if (!cards.length) return '<p style="color:var(--dim);font-size:12px;font-family:var(--font-mono);">No cards recorded.</p>';
    return '<div class="card-grid">' + cards.map(function(c) {
      return '<div class="card-chip">' + esc(c.name || c) + '</div>';
    }).join('') + '</div>';
  }

  var resultColor = b.result === 'win' ? 'ok' : (b.result === 'draw' ? 'warn' : 'err');
  document.getElementById('modal-body').innerHTML =
    '<div class="modal-meta">'
    + '<div class="modal-meta-item"><span>Time</span>'     + esc(formatBattleTime(b.battle_time)) + '</div>'
    + '<div class="modal-meta-item"><span>Player</span>'   + esc(b.player_name || b.player_tag) + '</div>'
    + '<div class="modal-meta-item"><span>Opponent</span>' + esc(b.opp_name || b.opp_tag || '—') + '</div>'
    + '<div class="modal-meta-item"><span>Result</span><strong style="color:var(--' + resultColor + ')">' + esc((b.result||'').toUpperCase()) + '</strong></div>'
    + '<div class="modal-meta-item"><span>Score</span>' + (b.team_crowns ?? '?') + ' – ' + (b.opp_crowns ?? '?') + '</div>'
    + (b.trophy_change != null ? '<div class="modal-meta-item"><span>Trophy Δ</span>' + (b.trophy_change >= 0 ? '+' : '') + esc(b.trophy_change) + '</div>' : '')
    + (b.type ? '<div class="modal-meta-item"><span>Mode</span>' + esc(b.type) + '</div>' : '')
    + '</div>'
    + '<div class="deck-section-title">Your Deck</div>' + cardGrid(teamCards)
    + '<div class="deck-section-title" style="margin-top:16px;">Opponent Deck</div>' + cardGrid(oppCards);

  document.getElementById('battle-modal').classList.add('open');
}

function closeModal() {
  document.getElementById('battle-modal').classList.remove('open');
}

// ── Harvest ───────────────────────────────────────────────────────────────────
async function triggerManualHarvest() {
  if (!confirm('Force snapshot generation? This will execute the daily loop immediately.')) return;
  try {
    var res  = await fetch('/admin/harvest/manual', { method: 'POST' });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    var data = await res.json();
    toast(data.message || 'Harvest triggered.', 'ok');
    appendLog('Manual harvest broadcast sent.', 'warn');
  } catch(e) {
    toast('Harvest failed: ' + e.message, 'err');
    appendLog('Harvest error: ' + e.message, 'err');
  }
}

// ── CSV Export ────────────────────────────────────────────────────────────────
async function handleCustomCSVExport() {
  var fields = Array.from(document.querySelectorAll('input[name="csv-fields"]:checked')).map(function(cb){ return cb.value; });
  if (!fields.length) { toast('Select at least one field.', 'err'); return; }

  var formData = new FormData();
  fields.forEach(function(f){ formData.append('fields', f); });
  formData.set('export_format', 'json');
  toast('Fetching data…', 'info');

  try {
    var res     = await fetch('/admin/export/custom', { method: 'POST', body: formData });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    var records = await res.json();
    if (!Array.isArray(records)) throw new Error('Invalid response format.');
    
    if (!records.length) { toast('No records returned.', 'err'); return; }

    var wantWinRate = document.getElementById('formula-winrate').checked;
    var wantWarPart = document.getElementById('formula-warpart').checked;

    records.forEach(function(row) {
      if (wantWinRate) {
        var w = row.totalWins || 0, l = row.totalLosses || 0;
        row['Computed_WinRate%'] = (w + l > 0) ? ((w / (w + l)) * 100).toFixed(1) : '0.0';
      }
      if (wantWarPart) {
        var used = row.decksUsedToday || 0, rem = row.decksRemaining || 0, total = used + rem;
        row['Computed_WarParticipation%'] = total > 0 ? ((used / total) * 100).toFixed(1) : '0.0';
      }
    });

    var headers = Object.keys(records[0]);
    var csvContent = headers.join(',') + '\n';
    records.forEach(function(row) {
      csvContent += headers.map(function(h) {
        var val = row[h] != null ? row[h] : 'N/A';
        return '"' + String(val).replace(/"/g, '""') + '"';
      }).join(',') + '\n';
    });

    var blob = new Blob([csvContent], { type: 'text/csv;charset=utf-8;' });
    var link = document.createElement('a');
    link.href = URL.createObjectURL(blob);
    link.setAttribute('download', 'Graveyard_Export.csv');
    link.style.display = 'none';
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    toast('CSV downloaded (' + records.length + ' rows).', 'ok');
  } catch(err) {
    toast('Export error: ' + err.message, 'err');
  }
}

// ── Admin ─────────────────────────────────────────────────────────────────────
async function confirmFlushCache() {
  if (!confirm('Flush all CR API cache keys from Redis?')) return;
  try {
    var res  = await fetch('/admin/flush-cache', { method: 'POST' });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    var data = await res.json();
    toast(data.message || 'Cache flushed.', 'ok');
    appendLog('Cache flushed.', 'warn');
    loadDiagnostics();
  } catch(e) {
    toast('Flush failed: ' + e.message, 'err');
    appendLog('Flush error: ' + e.message, 'err');
  }
}

// ═════════════════════════════════════════════════════════════════════════════
// UI EDITOR — improved
// ═════════════════════════════════════════════════════════════════════════════

function _editorSetButtons(hasContent) {
  var btns = ['btn-preview', 'btn-deploy', 'btn-reset', 'btn-diff'];
  btns.forEach(function(id) {
    var el = document.getElementById(id);
    if (el) el.disabled = !hasContent;
  });
}

function _editorUpdateStatusBar(opts) {
  var dot   = document.getElementById('sb-dot');
  var state = document.getElementById('sb-state');
  if (dot   && opts.dotClass) dot.className = 'sb-dot ' + opts.dotClass;
  if (state && opts.state)    state.textContent = opts.state;

  if (opts.template) {
    document.getElementById('sb-template').style.display   = '';
    document.getElementById('sb-tpl-name').textContent     = opts.template;
  }
  if (opts.source) {
    document.getElementById('sb-source-item').style.display = '';
    document.getElementById('sb-source').textContent        = opts.source;
  }
  if (opts.loadTime) {
    document.getElementById('sb-time-item').style.display  = '';
    document.getElementById('sb-load-time').textContent    = opts.loadTime;
  }
}

function _editorUpdateMetrics(text) {
  var chars = text.length;
  var lines = text ? text.split('\n').length : 0;
  document.getElementById('sb-chars-item').style.display = '';
  document.getElementById('sb-lines-item').style.display = '';
  document.getElementById('sb-chars').textContent = chars.toLocaleString();
  document.getElementById('sb-lines').textContent = lines.toLocaleString();
}

function onTemplateChange() {
  var ta = document.getElementById('editor-html-content');
  ta.value = '';
  editorOriginal   = '';
  editorDirty      = false;
  editorTemplateName = document.getElementById('editor-template-name').value;
  _editorSetButtons(false);
  _editorUpdateStatusBar({ dotClass: 'empty', state: 'No template loaded' });
  document.getElementById('sb-chars-item').style.display = 'none';
  document.getElementById('sb-lines-item').style.display = 'none';
  document.getElementById('sb-template').style.display   = 'none';
  document.getElementById('sb-source-item').style.display = 'none';
  document.getElementById('sb-time-item').style.display   = 'none';
  var dp = document.getElementById('diff-panel');
  if (dp) dp.classList.remove('open');
  document.getElementById('btn-diff').disabled = true;
}

async function fetchTemplateForEditor(source) {
  var name = document.getElementById('editor-template-name').value;
  var btn  = source === 'current' ? document.getElementById('btn-load-live') : document.getElementById('btn-load-default');
  btn.disabled = true;
  btn.textContent = '↻ Loading…';
  try {
    var res = await fetch('/admin/api/template/' + encodeURIComponent(name) + '?source=' + encodeURIComponent(source));
    if (!res.ok) throw new Error('HTTP ' + res.status);
    var data = await res.json();
    if (data.html === undefined) throw new Error('No HTML field in response.');

    var ta = document.getElementById('editor-html-content');
    ta.value       = data.html;
    editorOriginal = data.html;  
    editorDirty    = false;
    editorTemplateName = name;

    _editorSetButtons(true);
    _editorUpdateStatusBar({
      dotClass: 'loaded',
      state:    'Loaded — no unsaved changes',
      template: name,
      source:   source === 'current' ? 'Live DB' : 'Default file',
      loadTime: new Date().toLocaleTimeString(),
    });
    _editorUpdateMetrics(data.html);

    document.getElementById('diff-panel').classList.remove('open');
    document.getElementById('btn-diff').disabled = false;

    toast('Loaded ' + source + ' template: "' + name + '"', 'ok');
  } catch(e) {
    toast('Error loading template: ' + e.message, 'err');
  } finally {
    btn.disabled = false;
    btn.textContent = source === 'current' ? '↓ Load Live' : '↓ Load Default';
  }
}

document.addEventListener('DOMContentLoaded', function() {
  var ta = document.getElementById('editor-html-content');
  if (!ta) return;

  ta.addEventListener('input', function() {
    _editorUpdateMetrics(ta.value);
    if (!editorDirty && ta.value !== editorOriginal) {
      editorDirty = true;
      _editorUpdateStatusBar({ dotClass: 'dirty', state: 'Unsaved changes' });
    } else if (ta.value === editorOriginal) {
      editorDirty = false;
      _editorUpdateStatusBar({ dotClass: 'loaded', state: 'Loaded — no unsaved changes' });
    }
    if (document.getElementById('diff-panel').classList.contains('open')) {
      renderDiff(editorOriginal, ta.value);
    }
  });

  ta.addEventListener('keydown', function(e) {
    if (e.key === 'Tab') {
      e.preventDefault();
      var start = ta.selectionStart;
      var end   = ta.selectionEnd;
      ta.value  = ta.value.substring(0, start) + '  ' + ta.value.substring(end);
      ta.selectionStart = ta.selectionEnd = start + 2;
    }
  });

  loadDiagnostics();
});

async function deployTemplate() {
  if (editorDeploying) return;
  var name = document.getElementById('editor-template-name').value;
  var html = document.getElementById('editor-html-content').value.trim();
  if (!html) { toast('Nothing to deploy — editor is empty.', 'err'); return; }
  if (!confirm('Deploy this HTML as the live "' + name + '" template?')) return;

  editorDeploying = true;
  var btn = document.getElementById('btn-deploy');
  btn.disabled    = true;
  btn.textContent = '⏳ Deploying…';
  _editorUpdateStatusBar({ dotClass: 'deploying', state: 'Deploying…' });

  try {
    var body = new FormData();
    body.set('template_name', name);
    body.set('html_content',  html);
    var res = await fetch('/admin/update-html', { method: 'POST', body: body });
    if (!res.ok) throw new Error('HTTP ' + res.status);

    editorOriginal = html;
    editorDirty    = false;
    _editorUpdateStatusBar({ dotClass: 'loaded', state: 'Deployed successfully', loadTime: new Date().toLocaleTimeString() });
    toast('Deployed "' + name + '" successfully.', 'ok');
    appendLog('Template deployed: ' + name, 'ok');
  } catch(e) {
    _editorUpdateStatusBar({ dotClass: 'dirty', state: 'Deploy failed — changes unsaved' });
    toast('Deploy failed: ' + e.message, 'err');
    appendLog('Deploy error: ' + e.message, 'err');
  } finally {
    editorDeploying = false;
    btn.disabled    = false;
    btn.textContent = '🚀 Deploy';
  }
}

async function previewTemplate() {
  var html = document.getElementById('editor-html-content').value.trim();
  if (!html) { toast('Nothing to preview — editor is empty.', 'err'); return; }

  try {
    var body = new FormData();
    body.set('template_name', document.getElementById('editor-template-name').value);
    body.set('html_content',  html);
    var res  = await fetch('/admin/preview', { method: 'POST', body: body });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    var rendered = await res.text();
    var blob = new Blob([rendered], { type: 'text/html' });
    var url  = URL.createObjectURL(blob);
    var win  = window.open(url, '_blank');
    setTimeout(function() { URL.revokeObjectURL(url); }, 5000);
    if (!win) toast('Pop-up blocked — allow pop-ups for previews.', 'err');
  } catch(e) {
    toast('Preview failed: ' + e.message, 'err');
  }
}

async function resetTemplate() {
  var name = document.getElementById('editor-template-name').value;
  if (!confirm('Load the default for "' + name + '"? This will replace the editor content.')) return;
  await fetchTemplateForEditor('default');
  if (confirm('Default loaded. Deploy it to overwrite the live template?')) {
    await deployTemplate();
  }
}

function copyEditorToClipboard() {
  var ta = document.getElementById('editor-html-content');
  if (!ta.value) { toast('Nothing to copy.', 'err'); return; }
  navigator.clipboard.writeText(ta.value)
    .then(function() { toast('Copied to clipboard.', 'ok'); })
    .catch(function() {
      ta.select();
      document.execCommand('copy');
      toast('Copied to clipboard.', 'ok');
    });
}

// ── Diff viewer ───────────────────────────────────────────────────────────────
function toggleDiff() {
  var panel = document.getElementById('diff-panel');
  var isOpen = panel.classList.contains('open');
  if (isOpen) {
    panel.classList.remove('open');
    document.getElementById('btn-diff').textContent = '± Diff';
  } else {
    var current = document.getElementById('editor-html-content').value;
    renderDiff(editorOriginal, current);
    panel.classList.add('open');
    document.getElementById('btn-diff').textContent = '± Hide Diff';
  }
}

function renderDiff(original, current) {
  var container = document.getElementById('diff-content');
  if (!original && !current) {
    container.innerHTML = '<span class="diff-empty">Nothing to diff.</span>';
    return;
  }
  if (original === current) {
    container.innerHTML = '<span class="diff-empty">No changes — content matches the loaded version.</span>';
    return;
  }

  var aLines = original.split('\n');
  var bLines = current.split('\n');
  var result = lineDiff(aLines, bLines);
  var html   = result.map(function(d) {
    if (d.type === 'add') return '<div class="diff-line-add">+ ' + esc(d.line) + '</div>';
    if (d.type === 'del') return '<div class="diff-line-del">- ' + esc(d.line) + '</div>';
    return '<div class="diff-line-ctx">  ' + esc(d.line) + '</div>';
  }).join('');
  container.innerHTML = html || '<span class="diff-empty">Empty diff result.</span>';
}

function lineDiff(a, b) {
  var m = a.length, n = b.length;
  if (m > 2000 || n > 2000) {
    return [{ type: 'ctx', line: '(diff suppressed — file too large for inline diff)' }];
  }
  var dp = [];
  for (var i = 0; i <= m; i++) { dp[i] = new Array(n + 1).fill(0); }
  for (var i = 1; i <= m; i++) {
    for (var j = 1; j <= n; j++) {
      dp[i][j] = a[i-1] === b[j-1] ? dp[i-1][j-1] + 1 : Math.max(dp[i-1][j], dp[i][j-1]);
    }
  }
  var result = [], i = m, j = n;
  while (i > 0 || j > 0) {
    if (i > 0 && j > 0 && a[i-1] === b[j-1]) {
      result.unshift({ type: 'ctx', line: a[i-1] });
      i--; j--;
    } else if (j > 0 && (i === 0 || dp[i][j-1] >= dp[i-1][j])) {
      result.unshift({ type: 'add', line: b[j-1] });
      j--;
    } else {
      result.unshift({ type: 'del', line: a[i-1] });
      i--;
    }
  }
  var CONTEXT = 3;
  var changed = result.map(function(d, idx) { return d.type !== 'ctx' ? idx : -1; }).filter(function(x){ return x >= 0; });
  if (!changed.length) return [{ type: 'ctx', line: '(no changes)' }];
  var keep = new Set();
  changed.forEach(function(idx) {
    for (var k = Math.max(0, idx - CONTEXT); k <= Math.min(result.length - 1, idx + CONTEXT); k++) keep.add(k);
  });
  var out = [], prev = -1;
  result.forEach(function(d, idx) {
    if (!keep.has(idx)) { if (prev !== -2) { out.push({ type: 'ctx', line: '···' }); prev = -2; } return; }
    out.push(d); prev = idx;
  });
  return out;
}

document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape') closeModal();
});
</script>
</body>
</html>"""