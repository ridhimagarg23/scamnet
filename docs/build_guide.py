# build_guide.py
# ==============
# Generator for the interactive TraceAI developer guide
# (docs/interactive-guide.html).
#
# Run from the repository root:
#     python3 docs/build_guide.py
#
# The generator mixes:
#  * inline HTML/CSS/JS strings (the sections below), and
#  * LIVE values read from the real codebase, so the guide can't
#    silently drift from the source of truth:
#      - objective ladder & persona mappings are scraped from
#        tools/adaptive_investigation_engine.py
#      - risk rubric examples come from tools/risk_engine.py
#      - API contract notes come from backend/api.py comments
#
# Edit this file to change the guide, then re-run it.

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = Path(__file__).resolve().parent / "interactive-guide.html"


# ----------------------------------------------------------------------
# 1. Scrape live values from the codebase
# ----------------------------------------------------------------------

def read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def first_of(pattern, text, group=1):
    m = re.search(pattern, text)
    return m.group(group) if m else "?"


engine_src = read("tools/adaptive_investigation_engine.py")

# The ordered objective ladder (list after the "objectives = [" marker).
ladder_match = re.search(r'objectives\s*=\s*\[(.*?)\]', engine_src, re.S)
LADDER = re.findall(r'"([^"]+)"', ladder_match.group(1)) if ladder_match else []

# Strategy mapping {objective -> strategy}
STRATEGY_MAP = dict(re.findall(r'"([^"]+)":\s*"([^"]+)"', engine_src))

# Profile mapping: threat keyword -> InvestigationProfile(...) args
PROFILES = []
for kw, style, lang, lit in re.findall(
    r'if "(\w+)" in threat:\s*return InvestigationProfile\(\s*'
    r'communication_style="([^"]+)",\s*language="([^"]+)",\s*'
    r'digital_literacy="([^"]+)"',
    engine_src,
):
    PROFILES.append((kw, style, lang, lit))
FALLBACK_PROFILE = re.search(
    r"return InvestigationProfile\(\s*communication_style=\"([^\"]+)\",\s*language=\"([^\"]+)\",\s*digital_literacy=\"([^\"]+)\"",
    engine_src,
)

# First objectives per threat keyword {keyword -> objective}
FIRST_OBJECTIVES = dict(
    re.findall(
        r'if "(\w+)" in threat:\s*return "([^"]+)"',
        engine_src,
    )
)

risk_src = read("tools/risk_engine.py")

api_src = read("backend/api.py")


# ----------------------------------------------------------------------
# 2. HTML skeleton + CSS
# ----------------------------------------------------------------------

CSS = """
:root{--bg:#f8fafc;--card:#ffffff;--ink:#0f172a;--sub:#64748b;--line:#e2e8f0;
--brand:#16a34a;--brand-d:#15803d;--brand-l:#ecfdf5;--blue:#3b82f6;--blue-l:#eff6ff;
--violet:#7c3aed;--violet-l:#f5f3ff;--amber:#d97706;--amber-l:#fffbeb;
--red:#dc2626;--red-l:#fef2f2;--mono:'SFMono-Regular',ui-monospace,Consolas,monospace}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.6 -apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif}
.wrap{max-width:1060px;margin:0 auto;padding:28px 22px 80px}
h1{font-size:30px;letter-spacing:-.5px;margin:6px 0 4px}
h2{font-size:21px;margin:44px 0 6px;letter-spacing:-.3px}
h3{font-size:15px;margin:0 0 4px}
p.sub{color:var(--sub);margin-top:0}
header.hero{background:linear-gradient(135deg,#0f172a,#14532d);color:#fff;border-radius:18px;padding:30px 32px;margin-bottom:26px}
header.hero h1{margin:0}
header.hero p{color:#cbd5e1;margin:8px 0 0;max-width:720px}
.chips{display:flex;gap:8px;flex-wrap:wrap;margin-top:16px}
.chip{background:rgba(255,255,255,.12);border:1px solid rgba(255,255,255,.2);padding:4px 12px;border-radius:999px;font-size:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px;box-shadow:0 1px 2px rgba(15,23,42,.04)}
.grid{display:grid;gap:14px}
.grid.g2{grid-template-columns:repeat(auto-fit,minmax(300px,1fr))}
.k{font-weight:800}
.mono{font-family:var(--mono);font-size:12.5px;background:#f1f5f9;border:1px solid var(--line);border-radius:6px;padding:1px 6px;color:#334155;word-break:break-all}
pre{background:#0f172a;color:#e2e8f0;padding:16px 18px;border-radius:12px;overflow:auto;font-family:var(--mono);font-size:12.5px;line-height:1.55}
pre b{color:#86efac}
.lbl{font-size:10.5px;font-weight:800;letter-spacing:.08em;text-transform:uppercase;color:var(--sub)}
.badge{display:inline-block;font-size:10.5px;font-weight:700;padding:2px 9px;border-radius:999px}
.b-green{background:var(--brand-l);color:var(--brand-d)}
.b-blue{background:var(--blue-l);color:var(--blue)}
.b-violet{background:var(--violet-l);color:var(--violet)}
.b-amber{background:var(--amber-l);color:var(--amber)}
.b-red{background:var(--red-l);color:var(--red)}
.b-grey{background:#f1f5f9;color:#475569}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{text-align:left;padding:9px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--sub)}
tr:hover td{background:#f8fafc}
details{border:1px solid var(--line);border-radius:12px;padding:4px 16px;margin:10px 0;background:var(--card)}
details summary{cursor:pointer;font-weight:700;padding:10px 0;color:#1e293b}
details p{margin:6px 0 12px}
details pre{margin:0 0 12px}
.toc{columns:2;gap:26px;font-size:13.5px;background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px 24px;margin:18px 0 6px}
.toc a{color:#0f766e;text-decoration:none;font-weight:600}
.toc a:hover{text-decoration:underline}
.flow{display:flex;flex-direction:column;gap:10px;margin:14px 0}
.fnode{display:flex;gap:12px;align-items:flex-start;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px;cursor:pointer;transition:transform .15s,box-shadow .15s}
.fnode:hover{transform:translateX(4px);box-shadow:0 6px 16px rgba(15,23,42,.07)}
.fnode .idx{width:26px;height:26px;border-radius:8px;background:var(--brand-l);color:var(--brand-d);font-weight:800;display:flex;align-items:center;justify-content:center;flex-shrink:0;font-size:13px}
.fnode .idx.blue{background:var(--blue-l);color:var(--blue)}
.fnode .idx.violet{background:var(--violet-l);color:var(--violet)}
.fnode .idx.amber{background:var(--amber-l);color:var(--amber)}
.fnode h3{margin:0;font-size:14px}
.fnode p{margin:2px 0 0;color:var(--sub);font-size:12.5px}
.tree{font-family:var(--mono);font-size:13px;line-height:1.7;background:#f8fafc;border:1px solid var(--line);border-radius:12px;padding:16px 18px;overflow-x:auto}
.tree .t{color:#334155}.tree .c{color:#16a34a;font-weight:700}.tree .f{color:#7c3aed}.tree .m{color:#64748b}
button.pill{border:1.5px solid var(--line);background:var(--card);color:#334155;font-weight:700;font-size:12.5px;padding:6px 14px;border-radius:999px;cursor:pointer;transition:all .15s}
button.pill:hover{border-color:var(--brand);color:var(--brand-d);background:var(--brand-l)}
button.pill.on{background:var(--brand);border-color:var(--brand);color:#fff}
.rubric td:first-child,.rubric th:first-child{white-space:nowrap}
.hl{background:#fffbeb;border-left:4px solid var(--amber);padding:10px 14px;border-radius:0 10px 10px 0;font-size:13.5px;margin:14px 0}
.ok{background:#f0fdf4;border-left:4px solid var(--brand);padding:10px 14px;border-radius:0 10px 10px 0;font-size:13.5px;margin:14px 0}
footer{margin-top:60px;color:var(--sub);font-size:12.5px;text-align:center}
.slider{width:100%}
.legend{font-size:12px;color:var(--sub)}
"""

# ----------------------------------------------------------------------
# 3. Section builders (pure python -> html)
# ----------------------------------------------------------------------

def build_toc():
    return """
<div class="toc">
<a href="#pipeline">1&nbsp;Agent pipeline</a> ·
<a href="#flow">2&nbsp;End-to-end flow</a> ·
<a href="#personas">3&nbsp;Persona engine</a> ·
<a href="#ladder">4&nbsp;Objective ladder</a> ·
<a href="#risk">5&nbsp;Risk simulator</a> ·
<a href="#map">6&nbsp;Codebase map</a> ·
<a href="#contract">7&nbsp;API contract</a> ·
<a href="#glossary">8&nbsp;Glossary</a>
</div>
"""


def build_pipeline():
    agents = [
        ("1", "blue", "Investigation Agent", "agents/investigation_agent.py",
         "Investigates each message: regex IOC extraction -> URL analysis -> LLM verdict "
         "(is_scam / confidence / threat_type / summary) -> validation -> RiskEngine score. "
         "Returns a validated <span class='mono'>InvestigationResult</span>.",
         "prompts/investigation_prompt.txt"),
        ("2", "violet", "Adaptive Investigation Engine", "tools/adaptive_investigation_engine.py",
         "Deterministic 'strategy brain'. Picks the victim persona (language / style / digital "
         "literacy) from the threat family and walks an objective ladder. No LLM calls - fully "
         "unit-testable.",
         None),
        ("3", "blue", "Conversation Agent", "agents/conversation_agent.py",
         "Writes the persona's next chat reply. Stays in character, pursues the engine's current "
         "objective, replies in the scammer's language, < 35 words, never breaks cover.",
         "prompts/conversation_prompt.txt"),
        ("4", "amber", "Report Agent", "agents/report_agent.py",
         "Compiles verdict + IOCs + risk + honeypot dialogue into a professional markdown "
         "incident report (executive summary, risk assessment, recommendations...).",
         "prompts/report_prompt.txt"),
    ]
    cards = "".join(
        f"""
<details><summary><span class="badge b-{c}">AGENT {i}</span>
&nbsp; {name} &nbsp;<span class="mono">{file}</span></summary>
<p>{desc}</p>
<p>Prompt file: <span class="mono">{p or 'n/a (rule-based code)'}</span></p>
</details>"""
        for i, c, name, file, desc, p in agents
    )

    tools = [
        ("tools/entity_extractor.py", "Regex IOC extraction - Indian phones, emails, URLs, UPI handles, ₹ amounts, banks, OTP wording."),
        ("tools/url_checker.py", "Offline URL signals: HTTPS?, known shortener?, subdomain depth."),
        ("tools/risk_engine.py", "Weighted 0-100 score with human-readable reasons (LOW / MEDIUM / HIGH)."),
        ("tools/adaptive_investigation_engine.py", "Persona profile + objective/strategy state machine."),
        ("tools/memory_manager.py", "JSON-file archive: database/threat_memory.json (git-ignored)."),
        ("tools/conversation_session.py", "Per-session transcript serialised into the next LLM prompt."),
    ]
    tlist = "".join(
        f'<div style="margin:7px 0"><span class="mono">{f}</span><br><span style="color:var(--sub);font-size:12.5px">{d}</span></div>'
        for f, d in tools
    )

    return f"""
<h2 id="pipeline">1 &nbsp;Agent pipeline — who does what</h2>
<p class="sub">Click any card to expand. Every LLM-consuming agent is driven by an editable prompt in <span class="mono">prompts/</span>.</p>
<div class="grid g2">
<div>{cards}</div>
<div><div class="card"><div class="lbl">Deterministic support tools (no LLM → free, fast, testable)</div><div style="margin-top:6px">{tlist}</div></div></div>
</div>
"""


def build_flow():
    return """
<h2 id="flow">2 &nbsp;End-to-end flow — one /analyze request</h2>
<p class="sub">Click a stage to highlight it in the diagram below, then read the numbered notes.</p>
<div style="display:flex;gap:8px;flex-wrap:wrap" id="flowBtns"></div>
<div class="flow" id="flowDiagram"></div>
<div id="flowNote" class="ok" style="margin-top:16px"></div>
<script>
(function(){
  const stages=[
   {id:'s1',t:'Investigate',c:'blue',d:'InvestigationAgent extracts IOCs by regex, analyses URLs, asks the LLM for a verdict and computes a deterministic risk score (0-100 with reasons).'},
   {id:'s2',t:'Persona',c:'violet',d:'On the first turn the AdaptiveInvestigationEngine picks a decoy persona (language/style/literacy) matching the threat family (bank / job / investment / other).'},
   {id:'s3',t:'Engage',c:'blue',d:'ConversationAgent writes the persona\\'s reply pursuing the engine\\'s current objective. The reply is appended to the session transcript.'},
   {id:'s4',t:'Re-score',c:'amber',d:'Later turns merge newly extracted IOCs into the accumulated case, re-score risk with ALL evidence, and advance the engine to the next objective.'},
   {id:'s5',t:'Report',c:'amber',d:'ReportAgent compiles a markdown report; MemoryManager archives the case; the full dashboard JSON is returned.'}
  ];
  const box=document.getElementById('flowDiagram'),btns=document.getElementById('flowBtns'),note=document.getElementById('flowNote');
  stages.forEach((s,i)=>{
    const el=document.createElement('div');el.className='fnode';el.id=s.id;el.dataset.i=i;
    el.innerHTML='<div class="idx '+s.c+'">'+(i+1)+'</div><div><h3>'+s.t+'</h3><p>'+s.d+'</p></div>';
    box.appendChild(el);
    const b=document.createElement('button');b.className='pill';b.textContent='Stage '+(i+1);
    b.onclick=()=>{note.innerHTML='<b>Stage '+(i+1)+': '+s.t+'</b> — '+s.d;box.querySelectorAll('.fnode').forEach(n=>n.style.boxShadow='');el.style.boxShadow='0 0 0 3px var(--brand)';};
    btns.appendChild(b);
  });
  note.innerHTML='<b>Stage 1: Investigate</b> — '+stages[0].d;
  box.firstChild.style.boxShadow='0 0 0 3px var(--brand)';
})();
</script>
"""


def build_personas():
    rows = "".join(
        f"<tr><td><span class='mono'>\"{kw}\" in threat</span></td><td>{style}</td><td>{lang}</td><td>{lit}</td></tr>"
        for kw, style, lang, lit in PROFILES
    )
    first_rows = "".join(
        f"<tr><td><span class='mono'>\"{kw}\" in threat</span></td><td>{obj}</td></tr>"
        for kw, obj in FIRST_OBJECTIVES.items()
    )
    fb = FALLBACK_PROFILE
    fb_html = f"<span class='mono'>{fb.group(1)} / {fb.group(2)} / {fb.group(3)}</span>" if fb else "?"
    return f"""
<h2 id="personas">3 &nbsp;Persona engine — threat → cover identity</h2>
<p class="sub">Rule-based mapping inside <span class="mono">AdaptiveInvestigationEngine._select_profile()</span>. The API then pairs each profile with a named identity (e.g. banking → <em>Rahul Sharma, Working Professional</em>).</p>
<div class="card" style="overflow-x:auto">
<table>
<tr><th>Threat match (substring)</th><th>Communication style</th><th>Language</th><th>Digital literacy</th></tr>
{rows}
<tr><td><em>anything else (fallback)</em></td><td colspan="3">{fb_html}</td></tr>
</table>
</div>
<h3 style="margin-top:18px">First objective per threat family</h3>
<div class="card" style="overflow-x:auto">
<table>
<tr><th>Threat match</th><th>First objective (what the persona fishes for)</th></tr>
{first_rows}
</table>
</div>
"""


def build_ladder():
    steps = "".join(
        f"<li style='margin:6px 0'><span class='mono'>{i+1}. {o}</span> → strategy <span class='mono'>{STRATEGY_MAP.get(o,'Curious')}</span></li>"
        for i, o in enumerate(LADDER)
    )
    return f"""
<h2 id="ladder">4 &nbsp;Objective ladder — the persona's state machine</h2>
<p class="sub">Every <span class="mono">engine.update(objective_completed=True)</span> archives the current objective and activates the next one. Simulate a full investigation:</p>
<div class="card">
<div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
<button class="pill" id="advBtn">Advance to next objective</button>
<button class="pill" id="resetBtn">Reset</button>
<span id="ladderState" class="badge b-violet">turn 1</span>
</div>
<ol style="margin:14px 0 4px;padding-left:20px">{steps}</ol>
<div id="ladderDone" class="ok" style="display:none">🎉 All objectives completed — engine would answer <span class="mono">"End investigation"</span> and stay there (no loops).</div>
</div>
<script>
(function(){{
  const ladder={json.dumps(LADDER)};
  const strategies={json.dumps(STRATEGY_MAP)};
  let turn=1,done=[];
  const st=document.getElementById('ladderState'),dd=document.getElementById('ladderDone');
  const current=()=>ladder.find(o=>!done.includes(o))||'End investigation';
  function render(){{ st.textContent='turn '+turn+' → '+current()+' ('+(strategies[current()]||'Curious')+')'; dd.style.display=done.length>=ladder.length?'block':'none'; }}
  document.getElementById('advBtn').onclick=()=>{{ if(done.length<ladder.length){{ done.push(current()); }} turn++; render(); }};
  document.getElementById('resetBtn').onclick=()=>{{ turn=1; done=[]; render(); }};
  render();
}})();
</script>
"""


def build_risk():
    return """
<h2 id="risk">5 &nbsp;Risk simulator — play with RiskEngine</h2>
<p class="sub">Toggle signals and watch the score & reasons update (mirrors <span class="mono">tools/risk_engine.py</span> exactly).</p>
<div class="card">
<table class="rubric">
<tr><th>Signal</th><th style="width:170px">Pts</th><th>State</th></tr>
<tr><td><label><input type="checkbox" id="rScam" checked> LLM says <b>scam</b></label></td><td>+40</td><td><span class="badge b-red" id="s0">ON</span></td></tr>
<tr><td>Confidence <input type="range" id="rConf" min="0" max="100" value="90" style="width:110px;vertical-align:middle"> <span id="cVal" class="mono">90</span>%</td><td>+0…30 (×0.30)</td><td><span id="s1" class="badge b-blue">+27</span></td></tr>
<tr><td><label><input type="checkbox" id="rUrl" checked> Suspicious URL present</label></td><td>+10</td><td><span class="badge b-red" id="s2">ON</span></td></tr>
<tr><td><label><input type="checkbox" id="rHttps" checked> URL uses plain HTTP</label></td><td>+10</td><td><span class="badge b-red" id="s3">ON</span></td></tr>
<tr><td><label><input type="checkbox" id="rUpi"> UPI ID present</label></td><td>+10</td><td><span class="badge b-grey" id="s4">OFF</span></td></tr>
<tr><td><label><input type="checkbox" id="rPhone"> Phone number present</label></td><td>+5</td><td><span class="badge b-grey" id="s5">OFF</span></td></tr>
<tr><td><label><input type="checkbox" id="rEmail"> Email present</label></td><td>+5</td><td><span class="badge b-grey" id="s6">OFF</span></td></tr>
<tr><td><label><input type="checkbox" id="rShort"> Shortened URL (bit.ly…)</label></td><td>+10</td><td><span class="badge b-grey" id="s7">OFF</span></td></tr>
<tr><td><label><input type="checkbox" id="rSub"> ≥2 subdomains</label></td><td>+10</td><td><span class="badge b-grey" id="s8">OFF</span></td></tr>
<tr><td><label><input type="checkbox" id="rMulti"> ≥3 indicator families</label></td><td>+5</td><td><span class="badge b-grey" id="s9">OFF</span></td></tr>
</table>
<div style="display:flex;align-items:center;gap:18px;margin-top:16px">
<div style="font-size:44px;font-weight:800" id="bigScore">84</div>
<div style="flex:1">
<div style="height:10px;background:#f1f5f9;border-radius:6px;overflow:hidden"><div id="bar" style="height:100%;width:84%;background:var(--red);border-radius:6px;transition:all .3s"></div></div>
<div class="legend" style="margin-top:4px">0 ————— LOW ———— 50 ————— MEDIUM ———— 80 ————— HIGH ———— 100</div>
</div>
</div>
<p id="reasons" class="mono" style="margin:14px 0 0;font-size:12.5px;white-space:pre-line"></p>
</div>
<script>
(function(){
 const $=id=>document.getElementById(id);
 const checks=['rScam','rUrl','rHttps','rUpi','rPhone','rEmail','rShort','rSub','rMulti'];
 const pts=[40,10,10,10,5,5,10,10,5]; const names=['LLM detected a scam','Suspicious URL detected','Uses HTTP instead of HTTPS','UPI ID detected','Phone number detected','Email detected','Shortened URL detected','Suspicious subdomain','Multiple indicators detected'];
 const rows=[$('s0'),$('s2'),$('s3'),$('s4'),$('s5'),$('s6'),$('s7'),$('s8'),$('s9')];
 function recalc(){
   let score=0;const reasons=[];
   checks.forEach((c,i)=>{ if($(c).checked){score+=pts[i];reasons.push(names[i]);} });
   score+=Math.round(($('rConf').value*0.30));
   score=Math.min(score,100);
   const level=score>=80?'HIGH':score>=50?'MEDIUM':'LOW';
   const col=score>=80?'var(--red)':score>=50?'var(--amber)':'var(--brand)';
   $('bigScore').textContent=score; $('bar').style.width=score+'%'; $('bar').style.background=col;
   $('reasons').textContent=reasons.length?('Reasons:\\n• '+reasons.join('\\n• ')+'\\n\\nRisk level: '+level):'No reasons (score 0). Risk level: '+level;
 }
 checks.forEach(c=>$(c).addEventListener('change',recalc));
 $('rConf').addEventListener('input',()=>{ $('cVal').textContent=$('rConf').value; $('s1').textContent='+'+(Math.round($('rConf').value*0.30)); recalc(); });
 recalc();
})();
</script>
"""


def build_map():
    tree = """
<div class="tree">
<span class="m">TraceAI/</span><br>
├─ <span class="t">agents/</span>
│&nbsp;&nbsp;├─ <span class="f">investigation_agent.py</span>  <span class="m">verdict + IOCs + risk (LLM)</span><br>
│&nbsp;&nbsp;├─ <span class="f">conversation_agent.py</span>   <span class="m">decoy reply writer (LLM)</span><br>
│&nbsp;&nbsp;└─ <span class="f">report_agent.py</span>         <span class="m">markdown report (LLM)</span><br>
├─ <span class="t">backend/</span>
│&nbsp;&nbsp;└─ <span class="f">api.py</span>                  <span class="m">FastAPI: /analyze /new /health (session state)</span><br>
├─ <span class="t">tools/</span>
│&nbsp;&nbsp;├─ <span class="f">adaptive_investigation_engine.py</span> <span class="m">persona + objective brain</span><br>
│&nbsp;&nbsp;├─ <span class="f">entity_extractor.py</span>       <span class="m">regex IOC extraction</span><br>
│&nbsp;&nbsp;├─ <span class="f">risk_engine.py</span>           <span class="m">0-100 scoring</span><br>
│&nbsp;&nbsp;├─ <span class="f">url_checker.py</span>           <span class="m">URL signals</span><br>
│&nbsp;&nbsp;├─ <span class="f">memory_manager.py</span>        <span class="m">JSON archive</span><br>
│&nbsp;&nbsp;├─ <span class="f">conversation_session.py</span>   <span class="m">chat transcript</span><br>
│&nbsp;&nbsp;└─ <span class="f">prompt_loader.py</span>         <span class="m">loads prompts/*.txt</span><br>
├─ <span class="t">llm/</span><span class="f">llm_client.py</span>             <span class="m">single OpenRouter wrapper</span><br>
├─ <span class="t">utils/</span><span class="f">schemas.py</span>             <span class="m">Pydantic contracts</span><br>
├─ <span class="t">prompts/</span>                      <span class="m">agent system prompts (.txt)</span><br>
├─ <span class="t">frontend/</span>                     <span class="m">Next.js 14 dashboard</span><br>
│&nbsp;&nbsp;├─ <span class="f">app/page.jsx</span>             <span class="m">dashboard + API client</span><br>
│&nbsp;&nbsp;├─ <span class="f">components/</span>              <span class="m">panels & widgets</span><br>
│&nbsp;&nbsp;└─ <span class="f">lib/constants.js</span>         <span class="m">UI contract + IOC highlighter</span><br>
├─ <span class="t">tests/</span>                        <span class="m">offline unit tests (mocked LLM)</span><br>
├─ <span class="f">app.py</span>                       <span class="m">CLI single-shot run</span><br>
├─ <span class="f">config.py</span>                    <span class="m">env settings</span><br>
└─ <span class="c">database/threat_memory.json</span>   <span class="m">(git-ignored) archive</span>
</div>
"""
    return f"""
<h2 id="map">6 &nbsp;Codebase map</h2>
<p class="sub">One-line purpose for every meaningful path in the repository.</p>
{tree}
"""


def build_contract():
    return """
<h2 id="contract">7 &nbsp;API contract cheatsheet</h2>
<p class="sub">What the dashboard sends and receives. Response shapes are mirrored by <span class="mono">frontend/lib/constants.js → INITIAL_DASHBOARD_DATA</span>.</p>
<div class="grid g2">
<div>
<div class="lbl">POST /analyze — request</div>
<pre>{<b>"message"</b>: "Dear SBI customer, your card is blocked...",
  <b>"session_id"</b>: "session_abc123"}</pre>
<div class="lbl" style="margin-top:12px">POST /new — request</div>
<pre>{<b>"session_id"</b>: "session_abc123"}  → resets session state</pre>
<div class="lbl" style="margin-top:12px">GET /health</div>
<pre>{<b>"status"</b>: "healthy"}</pre>
</div>
<div>
<div class="lbl">Response — top-level keys</div>
<pre>{
  <b>"session_id"</b>: "...",
  <b>"investigation"</b>: {  riskScore, riskLevel,
      threatType, threatSeverity, confidenceScore,
      progress[], evidence[], activity[] },
  <b>"persona"</b>: {  name, occupation, initials,
      traits[6]{icon,label,value}, aiTip },
  <b>"conversation"</b>: {  reply, expected_outcome,
      messages[]{role,sender,time,content,link,status} },
  <b>"report"</b>: {  title, markdown }
}</pre>
</div>
</div>
"""


def build_glossary():
    return """
<h2 id="glossary">8 &nbsp;Glossary</h2>
<div class="card" style="overflow-x:auto">
<table>
<tr><th>Term</th><th>Meaning in TraceAI</th></tr>
<tr><td><span class="mono">IOC</span></td><td>Indicator of Compromise - a concrete artifact (URL, phone, UPI ID...) that links a message to scam infrastructure.</td></tr>
<tr><td><span class="mono">Honeypot</span></td><td>A decoy system used to attract and study attackers. Here: the persona chat channel that keeps the scammer talking.</td></tr>
<tr><td><span class="mono">Decoy persona</span></td><td>A fake victim identity (name, occupation, language...) generated to match the scam type.</td></tr>
<tr><td><span class="mono">Objective ladder</span></td><td>The ordered list of extraction goals the engine walks through (website → employee ID → payment method → stall → end).</td></tr>
<tr><td><span class="mono">Risk score</span></td><td>0-100 composite of LLM verdict + confidence + IOC signals, with written reasons for every point.</td></tr>
<tr><td><span class="mono">Session</span></td><td>One multi-turn undercover conversation, keyed by session_id, kept alive in memory by the API.</td></tr>
<tr><td><span class="mono">Threat memory</span></td><td>The JSON archive of finished investigations (database/threat_memory.json).</td></tr>
</table>
</div>
"""


# ----------------------------------------------------------------------
# 4. Assemble + write
# ----------------------------------------------------------------------

def main():
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TraceAI — Interactive Developer Guide</title>
<style>{CSS}</style>
</head>
<body>
<div class="wrap">

<header class="hero">
<div style="font-size:12px;letter-spacing:.18em;opacity:.75;font-weight:700">TRACEAI · DEVELOPER DOCUMENTATION</div>
<h1>🛡️ Undercover Scam Investigation — interactive guide</h1>
<p>TraceAI runs an automated undercover persona that engages scammers, extracts Indicators of Compromise, scores risk and writes incident reports. This guide walks through the whole system — click things, they respond.</p>
<div class="chips"><span class="chip">FastAPI backend</span><span class="chip">4 AI agents</span><span class="chip">6 deterministic tools</span><span class="chip">Next.js dashboard</span><span class="chip">OpenRouter · Qwen</span></div>
</header>

{build_toc()}

{build_pipeline()}

{build_flow()}

{build_personas()}

{build_ladder()}

{build_risk()}

{build_map()}

{build_contract()}

{build_glossary()}

<footer>Auto-generated by <span class="mono">docs/build_guide.py</span> · regenerate after pipeline changes: <span class="mono">python3 docs/build_guide.py</span></footer>
</div>
</body>
</html>"""

    OUT.write_text(html, encoding="utf-8")
    print(f"✓ wrote {OUT} ({len(html)} bytes)")


if __name__ == "__main__":
    main()
