"""Local HTTP viewer at http://localhost:8765 — chat input, MJPEG stream, action log."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

from .state import DriverState

INDEX_HTML = """<!doctype html><html><head><meta charset="utf-8">
<title>AgentFlow Desktop</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0b0d10;color:#e6e9ee;font-family:-apple-system,system-ui,sans-serif;height:100vh;overflow:hidden}
.wrap{display:grid;grid-template-columns:1.8fr 1fr;height:100vh}
.screen{background:#000;padding:8px;position:relative}
.screen img{width:100%;height:100%;object-fit:contain;border-radius:4px}
.cross{position:absolute;pointer-events:none;width:28px;height:28px;border:2px solid #7cffb2;border-radius:50%;box-shadow:0 0 0 5px rgba(124,255,178,.2);transform:translate(-50%,-50%);transition:top .2s,left .2s}
.side{background:#14181d;border-left:1px solid #2a2f37;display:flex;flex-direction:column;height:100vh}
.head{padding:14px 18px;border-bottom:1px solid #2a2f37;font-size:12px;color:#9aa3ad;font-family:'SF Mono',monospace;line-height:1.55}
.head .dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:#7cffb2;margin-right:8px;animation:pulse 1.5s infinite;vertical-align:middle}
.head .dot.busy{background:#ffc857}
.head .dot.idle{background:#5a6271;animation:none}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
.head .meta{color:#5a6271;margin-top:4px}
.log{flex:1;overflow-y:auto;padding:8px}
.row{padding:9px 12px;margin-bottom:6px;background:#1a1f26;border-left:3px solid #82b4ff;border-radius:4px;font-size:12.5px}
.row.thinking{border-left-color:#ffc857;background:#1c1a14}
.row.browser{border-left-color:#7cffb2}
.row.af{border-left-color:#82b4ff}
.row.tg{border-left-color:#82b4ff}
.row.done{border-left-color:#b69cff}
.row.start{border-left-color:#ff8aff;background:#1d141c}
.row .ts{color:#5a6271;font-family:'SF Mono',monospace;font-size:10.5px;margin-right:8px}
.row .act{font-family:'SF Mono',monospace;color:#7cffb2;font-weight:600}
.row.thinking .act{color:#ffc857}
.row.done .act{color:#b69cff}
.row.start .act{color:#ff8aff}
.row .detail{color:#c5c9d0;margin-top:4px;word-break:break-word;white-space:pre-wrap;font-family:'SF Mono',monospace;font-size:11.5px;line-height:1.5;max-height:160px;overflow-y:auto}
.row .thinking-text{color:#d4cba5;font-style:italic;margin-top:4px;line-height:1.5}
.composer{border-top:1px solid #2a2f37;background:#14181d;padding:10px 12px;display:flex;flex-direction:column;gap:8px}
.composer textarea{width:100%;min-height:64px;max-height:160px;background:#0e1216;color:#e6e9ee;border:1px solid #2a2f37;border-radius:6px;padding:10px 12px;font:13px/1.45 -apple-system,system-ui,sans-serif;resize:vertical;outline:none}
.composer textarea:focus{border-color:#7cffb2}
.composer .actions{display:flex;align-items:center;gap:8px;justify-content:space-between}
.composer button{background:#7cffb2;color:#000;border:none;padding:7px 16px;border-radius:5px;font-weight:600;cursor:pointer;font-size:13px}
.composer button:disabled{background:#2a2f37;color:#5a6271;cursor:not-allowed}
.composer .hint{color:#5a6271;font-size:11px;font-family:'SF Mono',monospace}
.composer .preset{background:#1a1f26;border:1px solid #2a2f37;color:#9aa3ad;padding:5px 10px;border-radius:4px;cursor:pointer;font-size:11.5px}
.composer .preset:hover{border-color:#7cffb2;color:#e6e9ee}
.composer .presets{display:flex;flex-wrap:wrap;gap:6px;max-height:120px;overflow-y:auto}
</style></head><body>
<div class="wrap">
  <div class="screen"><img id="shot" src="/stream.mjpg"><div class="cross" id="cross" style="display:none"></div></div>
  <div class="side">
    <div class="head"><span class="dot idle" id="dot"></span><span id="status">idle</span><div class="meta" id="meta">claude-opus-4-7 · Mac + Chromium + AgentFlow API</div></div>
    <div class="log" id="log"></div>
    <div class="composer">
      <div class="presets" id="presets"></div>
      <textarea id="task" placeholder="Что должен сделать AI? (Cmd+Enter = отправить)"></textarea>
      <div class="actions">
        <span class="hint" id="hint">ready</span>
        <button id="send">Отправить</button>
      </div>
    </div>
  </div>
</div>
<script>
let PRESETS=[];
async function loadPresets(){
  try{
    const r=await fetch('/presets.json');
    PRESETS=await r.json();
  }catch(e){PRESETS=[];}
  const c=document.getElementById('presets');
  c.innerHTML=PRESETS.map((p,i)=>`<span class="preset" data-i="${i}" title="${(p.task||'').replace(/"/g,'&quot;')}">${p.label}</span>`).join('');
  c.querySelectorAll('.preset').forEach(el=>el.addEventListener('click',()=>{
    document.getElementById('task').value=PRESETS[parseInt(el.dataset.i)].task;
    document.getElementById('task').focus();
  }));
}
loadPresets();
async function send(){
  const ta=document.getElementById('task');
  const task=ta.value.trim();
  if(!task)return;
  const btn=document.getElementById('send');
  btn.disabled=true;document.getElementById('hint').textContent='отправлено';
  try{
    await fetch('/task',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({task})});
    ta.value='';
  }catch(e){document.getElementById('hint').textContent='ошибка: '+e.message}
  setTimeout(()=>{btn.disabled=false;document.getElementById('hint').textContent='ready'},800);
}
document.getElementById('send').addEventListener('click',send);
document.getElementById('task').addEventListener('keydown',e=>{
  if((e.metaKey||e.ctrlKey)&&e.key==='Enter'){e.preventDefault();send();}
});
async function tick(){
  const t=Date.now();
  const img=document.getElementById('shot');
  try{
    const [actionsR,stateR]=await Promise.all([fetch('actions.json?_='+t),fetch('state.json?_='+t)]);
    const data=await actionsR.json();const state=await stateR.json();
    const log=document.getElementById('log');
    log.innerHTML=data.slice().reverse().map(a=>{
      let cls='row';
      if(a.action==='thinking')cls='row thinking';
      else if(a.action==='start')cls='row start';
      else if(a.action.startsWith('browser_'))cls='row browser';
      else if(a.action.startsWith('af_'))cls='row af';
      else if(a.action.startsWith('tg_'))cls='row tg';
      else if(a.action==='DONE'||a.action==='task_complete')cls='row done';
      const th=a.thinking?`<div class="thinking-text">${a.thinking}</div>`:'';
      const d=a.detail?`<div class="detail">${a.detail}</div>`:'';
      return `<div class="${cls}"><span class="ts">${a.ts}</span><span class="act">${a.action}</span>${th}${d}</div>`;
    }).join('');
    const dot=document.getElementById('dot');
    if(state.busy){dot.className='dot busy';document.getElementById('status').textContent='работает: '+(state.current_task||'').slice(0,40);}
    else{dot.className='dot idle';document.getElementById('status').textContent='idle · отправь задачу ↓';}
    document.getElementById('meta').textContent=`tasks done: ${state.task_count} · steps in log: ${data.length}`;
    const last=data[data.length-1];
    if(last&&last.cursor&&last.action==='mouse_click'){
      const cross=document.getElementById('cross');
      const rect=img.getBoundingClientRect();
      const iw=img.naturalWidth,ih=img.naturalHeight;
      if(iw>0&&ih>0){
        const scale=Math.min(rect.width/iw,rect.height/ih);
        const offX=(rect.width-iw*scale)/2,offY=(rect.height-ih*scale)/2;
        cross.style.left=(rect.left+offX+last.cursor[0]*scale)+'px';
        cross.style.top=(rect.top+offY+last.cursor[1]*scale)+'px';
        cross.style.display='block';
        clearTimeout(window._ct);window._ct=setTimeout(()=>{cross.style.display='none'},2500);
      }
    }
  }catch(e){}
}
tick();setInterval(tick,600);
</script></body></html>"""


def make_handler(state: DriverState, presets: list[dict[str, str]]) -> type[BaseHTTPRequestHandler]:
    """Build a request handler class bound to a state + preset library."""

    class H(BaseHTTPRequestHandler):
        def log_message(self, *args: object, **kwargs: object) -> None:  # noqa: D401
            pass

        def _json(self, obj: object, code: int = 200) -> None:
            data = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/" or self.path.startswith("/?"):
                self.send_response(200)
                self.send_header("content-type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(INDEX_HTML.encode("utf-8"))
                return
            if self.path.startswith("/stream.mjpg"):
                self._serve_mjpeg()
                return
            if self.path.startswith("/latest.jpg"):
                with state.stream_cond:
                    frame = state.stream_frame["jpeg"]
                if not frame:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("content-type", "image/jpeg")
                self.send_header("content-length", str(len(frame)))
                self.end_headers()
                self.wfile.write(frame)
                return
            if self.path.startswith("/actions.json"):
                with state.actions_lock:
                    payload = json.dumps(state.actions, ensure_ascii=False).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.end_headers()
                self.wfile.write(payload)
                return
            if self.path.startswith("/state.json"):
                self._json(
                    {
                        "busy": state.busy,
                        "current_task": state.current_task,
                        "task_count": state.task_count,
                        "queue_size": state.task_queue.qsize(),
                    }
                )
                return
            if self.path.startswith("/presets.json"):
                self._json(presets)
                return
            self.send_response(404)
            self.end_headers()

        def do_POST(self) -> None:  # noqa: N802
            if self.path == "/task":
                ln = int(self.headers.get("content-length", "0"))
                raw = self.rfile.read(ln).decode("utf-8") if ln else "{}"
                try:
                    payload = json.loads(raw)
                    task = (payload.get("task") or "").strip()
                    if not task:
                        self._json({"ok": False, "error": "empty task"}, 400)
                        return
                    task_id = state.enqueue_task(task)
                    self._json(
                        {
                            "ok": True,
                            "queued": True,
                            "task_id": task_id,
                            "queue_size": state.task_queue.qsize(),
                        }
                    )
                except Exception as exc:  # noqa: BLE001
                    self._json({"ok": False, "error": str(exc)}, 400)
                return
            self.send_response(404)
            self.end_headers()

        def _serve_mjpeg(self) -> None:
            boundary = b"--agentflow-frame"
            try:
                self.send_response(200)
                self.send_header("age", "0")
                self.send_header("cache-control", "no-cache, no-store, must-revalidate, private")
                self.send_header("pragma", "no-cache")
                self.send_header(
                    "content-type", "multipart/x-mixed-replace; boundary=agentflow-frame"
                )
                self.end_headers()
                last_ts = 0.0
                while True:
                    with state.stream_cond:
                        state.stream_cond.wait_for(
                            lambda lt=last_ts: state.stream_frame["ts"] > lt, timeout=2.0
                        )
                        frame = state.stream_frame["jpeg"]
                        last_ts = state.stream_frame["ts"]
                    if not frame:
                        continue
                    self.wfile.write(boundary + b"\r\n")
                    self.wfile.write(b"Content-Type: image/jpeg\r\n")
                    self.wfile.write(f"Content-Length: {len(frame)}\r\n\r\n".encode())
                    self.wfile.write(frame)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                return

    return H


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def start_viewer(
    state: DriverState,
    presets: list[dict[str, str]],
    port: int = 8765,
    host: str = "127.0.0.1",
) -> ThreadedHTTPServer:
    """Start the HTTP viewer in a background thread. Returns the server (call shutdown to stop)."""
    handler = make_handler(state, presets)
    srv = ThreadedHTTPServer((host, port), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
