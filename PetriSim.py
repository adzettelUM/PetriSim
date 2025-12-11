# petri_server.py
import json
import threading
import time
import webbrowser
import os
from copy import deepcopy
from typing import Dict, List, Tuple, Any

from flask import Flask, jsonify, request, send_file
from flask_socketio import SocketIO, emit
from pyvis.network import Network

import re


# --- Petri Net Core ---
class PetriNet:
    def __init__(self, filename = "sample.txt"):
        self.places = {}
        self.transitions = []
        self.arcs_in = {}
        self.arcs_out = {}
        self.load_petri_net(filename)

    def fireable(self):
        fireable = []
        for t, arcs in self.arcs_in.items():
            if all(self.places[p] >= w for p, w in arcs):
                fireable.append(t)
        return fireable

    def fire(self, transition):
        if transition not in self.fireable():
            return False
        for p, w in self.arcs_in[transition]:
            self.places[p] -= w
        for p, w in self.arcs_out[transition]:
            self.places[p] += w
        return True

    def load_petri_net(self, filename):
        
        section = None

        arc_re = re.compile(r"(\S+)\s*->\s*(\S+)\s+(\d+)")

        with open(filename) as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue

                # Section headers
                if line in ("PLACES", "TRANSITIONS", "ARCS"):
                    section = line
                    continue

                # Parse each section
                if section == "PLACES":
                    name, tokens = line.split()
                    self.places[name] = int(tokens)

                elif section == "TRANSITIONS":
                    self.transitions.append(line)

                elif section == "ARCS":
                    m = arc_re.match(line)
                    if not m:
                        raise ValueError(f"Invalid arc syntax: {line}")

                    src, dst, w = m.groups()
                    w = int(w)

                    # place -> transition
                    if src in self.places:
                        self.arcs_in.setdefault(dst, []).append((src, w))
                    # transition -> place
                    else:
                        self.arcs_out.setdefault(src, []).append((dst, w))

# -----------------------
# PetriNetServer class
# -----------------------
class PetriNetServer:
    """
    Single-class Petri net web visualizer + server.

    Usage:
        pn = PetriNet() 
        server = PetriNetServer(pn)
        server.start(open_browser=True)

    Minimal PetriNet interface required:
        pn.places: Dict[place_name -> int tokens]
        pn.transitions: List[transition_name]
        pn.arcs_in: Dict[transition -> List[(place, weight)]]
        pn.arcs_out: Dict[transition -> List[(place, weight)]]
        pn.fireable() -> List[str] (names of fireable transitions)
        pn.fire(transition_name) -> bool (fires and returns True if fired)
    """

    def __init__(self, pn, html_filename="petri_net.html", host="127.0.0.1", port=5000):
        self.pn = pn
        self.html_filename = html_filename
        self.host = host
        self.port = port

        # snapshot for Option A: reset tokens only
        self.initial_marking = dict(self.pn.places)

        # history for undo/redo (store markings)
        self.history: List[Dict[str, int]] = [deepcopy(self.pn.places)]
        self.history_index = 0

        # flask + socketio
        self.app = Flask(__name__, static_folder=".")
        # allow websocket cross-origin for local use
        self.socketio = SocketIO(self.app, cors_allowed_origins="*", async_mode="eventlet")

        # pyvis network container
        self.net = Network(directed=True, notebook=False, height="100vh", width="100vw")
        self.place_nodes: Dict[str, str] = {}
        self.transition_nodes: Dict[str, str] = {}
        self.built = False

        # concurrency lock
        self.lock = threading.Lock()

        # prepare routes & socket handlers
        self._setup_routes()
        self._setup_socket_handlers()

    # -------------------
    # Graph building
    # -------------------
    def build_graph(self):
        """
        Build or rebuild pyvis Network object from current pn state.
        """
        # Recreate Network to ensure a clean template each time
        self.net = Network(directed=True, notebook=False, height="100vh", width="100vw")

        # Places
        for place, tokens in self.pn.places.items():
            nid = f"p_{place}"
            label = f"{place}\nTokens: {tokens}"
            self.net.add_node(
                nid,
                label=label,
                shape="circle",
                color="#89CFF0",
                size=30 + tokens * 5,
                title=f"Place {place}"
            )
            self.place_nodes[place] = nid

        # Transitions (boxes) - use width/height for rectangles
        for t in self.pn.transitions:
            nid = f"t_{t}"
            color = "#7CFC00" if t in self.pn.fireable() else "#A9A9A9"
            self.net.add_node(
                nid,
                label=t,
                shape="box",
                color=color,
                width=110,
                height=60,
                title="Click to fire"
            )
            self.transition_nodes[t] = nid

        # Edges
        for t, arcs in self.pn.arcs_in.items():
            for place, w in arcs:
                self.net.add_edge(self.place_nodes[place], self.transition_nodes[t], label=str(w), smooth="curvedCW")
        for t, arcs in self.pn.arcs_out.items():
            for place, w in arcs:
                self.net.add_edge(self.transition_nodes[t], self.place_nodes[place], label=str(w), smooth="curvedCCW")

        # options for nicer behavior
        options = """
        var options = {
          "physics": {
            "stabilization": false
          },
          "interaction": { "hover": true, "multiselect": false }
        }
        """
        self.net.set_options(options)
        self.built = True

    def update_nodes_payload(self):
        """
        Produce list of nodes data for client update.
        Each node: {id,label,color,size,width,height}
        """
        nodes = []
        for place, tokens in self.pn.places.items():
            nid = self.place_nodes[place]
            nodes.append({
                "id": nid,
                "label": f"{place}\nTokens: {tokens}",
                "color": "#89CFF0",
                "size": 30 + tokens * 5,
                "width": None,
                "height": None
            })
        for t in self.pn.transitions:
            nid = self.transition_nodes[t]
            nodes.append({
                "id": nid,
                "label": t,
                "color": "#7CFC00" if t in self.pn.fireable() else "#A9A9A9",
                "size": None,
                "width": 110,
                "height": 60
            })
        return nodes

    # -------------------
    # HTML + JS injection
    # -------------------
    def save_html_with_frontend(self):
        """
        Save pyvis HTML and inject our JS frontend which:
          - connects Socket.IO
          - handles clicks on transitions (emit 'fire' event)
          - handles toolbar actions (emit appropriate events)
          - receives 'update' events and applies updates to the network
          - shows DEADLOCK banner when server indicates deadlock
        """
        self.build_graph()
        # write base HTML
        self.net.save_graph(self.html_filename)

        # load HTML and insert our JS
        with open(self.html_filename, "r", encoding="utf-8") as f:
            html = f.read()

        # Make sure container fills viewport
        html = html.replace("height: 500px;", "height: 100vh;").replace("width: 500px;", "width: 100vw;")

        # Build injected HTML: toolbar + deadlock banner + socket io scripts + handlers
        injected_frontend = f"""
<!-- TOOLBAR -->
<div id="toolbar" style="
    position: fixed;
    top: 10px;
    left: 10px;
    z-index: 9999;
    background: rgba(255,255,255,0.9);
    padding: 8px;
    border-radius: 8px;
    box-shadow: 0 2px 8px rgba(0,0,0,0.15);
    display:flex;
    gap:6px;
    align-items:center;
">
  <button onclick="emitStep()" title="Step (fire selected transition)">Step</button>
  <button onclick="emitRunToDeadlock()" title="Run until deadlock">Run to Deadlock</button>
  <button onclick="emitUndo()" title="Undo">Undo</button>
  <button onclick="emitRedo()" title="Redo">Redo</button>
  <button onclick="emitReset()" title="Reset tokens">Reset</button>
  <button onclick="saveNet()" title="Save net to JSON">Save</button>
  <input id="loadFile" type="file" style="display:none" onchange="loadNetFromFile(event)" />
  <button onclick="document.getElementById('loadFile').click()" title="Load net from JSON">Load</button>
  <button onclick="zoomToFit()" title="Zoom to fit">Zoom to Fit</button>
  <span id="status" style="margin-left:6px;font-weight:600;"></span>
</div>

<!-- DEADLOCK BANNER (hidden by default) -->
<div id="deadlockBanner" style="
    display:none;
    position: fixed;
    top: 0;
    left: 0;
    width: 100%;
    background: red;
    color: white;
    text-align: center;
    padding: 18px;
    font-size: 28px;
    font-weight: 700;
    z-index: 9998;
    cursor: pointer;
">
    DEADLOCK — CLICK TO RESET
</div>

<script src="//cdnjs.cloudflare.com/ajax/libs/socket.io/4.6.1/socket.io.min.js"></script>
<script type="text/javascript">
  var socket = io();

  // network variable is created by pyvis as 'network'
  // helper to update node (circle or box)
  function applyNodeUpdate(n) {{
    let update = {{ id: n.id, label: n.label, color: n.color }};
    if(n.size !== null) {{
      update.size = n.size;
    }}
    if(n.width !== null) {{
      update.width = n.width;
      update.height = n.height;
    }}
    try {{
      network.body.data.nodes.update(update);
    }} catch(e) {{
      console.error("update error", e, update);
    }}
  }}

  // When server sends update, apply nodes and deadlock/banner state
  socket.on("update", function(payload) {{
    if(payload.nodes) {{
      payload.nodes.forEach(function(n) {{ applyNodeUpdate(n); }});
    }}
    if(payload.deadlocked) {{
      document.getElementById('deadlockBanner').style.display = 'block';
    }} else {{
      document.getElementById('deadlockBanner').style.display = 'none';
    }}
    if(payload.message) {{
      document.getElementById('status').innerText = payload.message;
      setTimeout(()=>document.getElementById('status').innerText='', 1500);
    }}
  }});

  // handle click on network nodes
  network.on("click", function(params) {{
    if(params.nodes && params.nodes.length === 1) {{
      var nodeId = params.nodes[0];
      if(nodeId.startsWith("t_")) {{
        // trigger server fire of this transition
        socket.emit("fire", {{ transition: nodeId.substring(2) }});
      }}
    }}
  }});

  // deadlock banner click triggers reset
  document.getElementById("deadlockBanner").onclick = function() {{
    socket.emit("reset");
  }};

  // Toolbar functions
  function emitStep() {{
    // Step: fire currently selected transition (if any)
    var sel = network.getSelectedNodes();
    if(sel && sel.length === 1 && sel[0].startsWith("t_")) {{
      socket.emit("fire", {{ transition: sel[0].substring(2) }});
    }} else {{
      // no selection: fire the first available transition
      socket.emit("step");
    }}
  }}
  function emitRunToDeadlock() {{ socket.emit("run_to_deadlock"); }}
  function emitUndo() {{ socket.emit("undo"); }}
  function emitRedo() {{ socket.emit("redo"); }}
  function emitReset() {{ socket.emit("reset"); }}
  function zoomToFit() {{ network.fit(); }}

  // Save/Load
  function saveNet() {{
    socket.emit("save_request");
  }}
  socket.on("save_data", function(payload) {{
    var blob = new Blob([JSON.stringify(payload, null, 2)], {{type: "application/json"}});
    var url = URL.createObjectURL(blob);
    var a = document.createElement("a");
    a.href = url;
    a.download = "petri_net.json";
    a.click();
    URL.revokeObjectURL(url);
  }});

  function loadNetFromFile(evt) {{
    var file = evt.target.files[0];
    if(!file) return;
    var reader = new FileReader();
    reader.onload = function(e) {{
      try {{
        var obj = JSON.parse(e.target.result);
        socket.emit("load_request", obj);
      }} catch(err) {{
        alert("Invalid JSON file");
      }}
    }};
    reader.readAsText(file);
  }}

  // network ready -> request full update
  socket.on("connect", function() {{
    socket.emit("client_ready");
  }});

</script>
"""
        # Insert banner + toolbar and socket JS before </body>
        if "</body>" in html:
            html = html.replace("</body>", injected_frontend + "</body>")
        else:
            html += injected_frontend

        # write back
        with open(self.html_filename, "w", encoding="utf-8") as f:
            f.write(html)

    # -------------------
    # Flask routes
    # -------------------
    def _setup_routes(self):
        @self.app.route("/")
        def index():
            # ensure latest HTML written
            with self.lock:
                self.save_html_with_frontend()
            return send_file(self.html_filename)

        @self.app.route("/save_net.json", methods=["GET"])
        def save_net_endpoint():
            # return JSON representation of net
            data = self._serialize_pn()
            return jsonify(data)

    # -------------------
    # Socket handlers
    # -------------------
    def _setup_socket_handlers(self):
        @self.socketio.on("client_ready")
        def handle_client_ready():
            # send full node update
            with self.lock:
                nodes = self.update_nodes_payload()
                deadlocked = len(self.pn.fireable()) == 0
            emit("update", {"nodes": nodes, "deadlocked": deadlocked})

        @self.socketio.on("fire")
        def handle_fire(message):
            t = message.get("transition")
            if t is None:
                emit("update", {"message": "No transition specified."})
                return
            with self.lock:
                fired = False
                try:
                    fired = self.pn.fire(t)
                except Exception as e:
                    emit("update", {"message": f"Error firing: {e}"})
                    return
                # push to history (only if fired)
                if fired:
                    self._push_history()
                nodes = self.update_nodes_payload()
                deadlocked = len(self.pn.fireable()) == 0
            emit("update", {"nodes": nodes, "deadlocked": deadlocked, "fired": fired, "message": f"Fired {t}"})

        @self.socketio.on("step")
        def handle_step():
            # fire the first available transition if any
            with self.lock:
                avail = self.pn.fireable()
                if not avail:
                    emit("update", {"message": "No transitions fireable."})
                    return
                fired = self.pn.fire(avail[0])
                if fired:
                    self._push_history()
                nodes = self.update_nodes_payload()
                deadlocked = len(self.pn.fireable()) == 0
            emit("update", {"nodes": nodes, "deadlocked": deadlocked, "fired": fired, "message": f"Fired {avail[0]}"})

        @self.socketio.on("run_to_deadlock")
        def handle_run():
            with self.lock:
                steps = 0
                # simple strategy: always pick first fireable until none remain
                while True:
                    avail = self.pn.fireable()
                    if not avail:
                        break
                    self.pn.fire(avail[0])
                    self._push_history()
                    steps += 1
                    if steps > 10000:
                        break
                nodes = self.update_nodes_payload()
                deadlocked = len(self.pn.fireable()) == 0
            emit("update", {"nodes": nodes, "deadlocked": deadlocked, "message": f"Ran {steps} steps"})

        @self.socketio.on("undo")
        def handle_undo():
            with self.lock:
                if self.history_index <= 0:
                    emit("update", {"message": "Nothing to undo."})
                    return
                self.history_index -= 1
                self.pn.places = deepcopy(self.history[self.history_index])
                nodes = self.update_nodes_payload()
                deadlocked = len(self.pn.fireable()) == 0
            emit("update", {"nodes": nodes, "deadlocked": deadlocked, "message": "Undone"})

        @self.socketio.on("redo")
        def handle_redo():
            with self.lock:
                if self.history_index >= len(self.history) - 1:
                    emit("update", {"message": "Nothing to redo."})
                    return
                self.history_index += 1
                self.pn.places = deepcopy(self.history[self.history_index])
                nodes = self.update_nodes_payload()
                deadlocked = len(self.pn.fireable()) == 0
            emit("update", {"nodes": nodes, "deadlocked": deadlocked, "message": "Redone"})

        @self.socketio.on("reset")
        def handle_reset():
            with self.lock:
                self.pn.places = dict(self.initial_marking)
                self._push_history()
                nodes = self.update_nodes_payload()
                deadlocked = len(self.pn.fireable()) == 0
            emit("update", {"nodes": nodes, "deadlocked": deadlocked, "message": "Reset to initial marking"})

        @self.socketio.on("save_request")
        def handle_save_request():
            # server will respond with serialized JSON as a socket event
            data = self._serialize_pn()
            emit("save_data", data)

        @self.socketio.on("load_request")
        def handle_load_request(data):
            # expected to receive a JSON object matching _serialize_pn format
            with self.lock:
                try:
                    self._load_from_serialized(data)
                    # reset history on load
                    self.history = [deepcopy(self.pn.places)]
                    self.history_index = 0
                    nodes = self.update_nodes_payload()
                    deadlocked = len(self.pn.fireable()) == 0
                    emit("update", {"nodes": nodes, "deadlocked": deadlocked, "message": "Loaded net"})
                except Exception as e:
                    emit("update", {"message": f"Failed to load net: {e}"})

    # -------------------
    # Helpers: history + serialization
    # -------------------
    def _push_history(self):
        # push current marking to history and clamp length
        # if we undid some steps and then push a new state, trim the redo branch
        self.history = self.history[: self.history_index + 1]
        self.history.append(deepcopy(self.pn.places))
        self.history_index += 1
        # optional: cap history length
        if len(self.history) > 200:
            self.history = self.history[-200:]
            self.history_index = len(self.history) - 1

    def _serialize_pn(self):
        return {
            "places": deepcopy(self.pn.places),
            "transitions": deepcopy(self.pn.transitions),
            "arcs_in": deepcopy(self.pn.arcs_in),
            "arcs_out": deepcopy(self.pn.arcs_out)
        }

    def _load_from_serialized(self, obj: dict):
        # minimal validation
        assert "places" in obj and "transitions" in obj and "arcs_in" in obj and "arcs_out" in obj
        self.pn.places = dict(obj["places"])
        self.pn.transitions = list(obj["transitions"])
        self.pn.arcs_in = deepcopy(obj["arcs_in"])
        self.pn.arcs_out = deepcopy(obj["arcs_out"])
        # snapshot initial marking for reset
        self.initial_marking = dict(self.pn.places)

    # -------------------
    # Public API: start/stop
    # -------------------
    def start(self, open_browser=True):
        # prepare initial HTML
        with self.lock:
            self.save_html_with_frontend()

        # open browser after slight delay to allow server to start
        if open_browser:
            def _open():
                time.sleep(0.5)
                webbrowser.open(f"http://{self.host}:{self.port}/")
            threading.Thread(target=_open, daemon=True).start()

        print(f"Starting server at http://{self.host}:{self.port}/")
        # Run with socketio (uses eventlet by default if installed)
        # use allow_unsafe_werkzeug only if needed; prefer socketio.run
        self.socketio.run(self.app, host=self.host, port=self.port, debug=False, use_reloader=False)

    def stop(self):
        # Not implemented: rely on killing process
        pass

# -----------------------
# Minimal example PetriNet for testing
# -----------------------
if __name__ == "__main__":
    
    pn = PetriNet()
    server = PetriNetServer(pn)
    server.start(open_browser=True)
