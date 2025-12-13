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

from collections import deque
import copy

# --- Petri Net Core ---
class PetriNet:
    # Constructor
    def __init__(self, filename = "sample.txt"):
        self.places = {}        # The places of the Petri net; dynamically updates via firing transitions
        self.transitions = []   # The transitions of the Petri net
        self.arcs_in = {}       # The arcs from transitions to places
        self.arcs_out = {}      # The arcs form places to transitions
        self.load_petri_net(filename)   # Default loading of sample Petri Net
    # Returns list of all fireable transitions
    def fireable(self):
        fireable = []
        for t, arcs in self.arcs_in.items():
            can_fire = True
            for p, w in arcs:
                if(self.places[p] == 'w'):
                    continue
                if(self.places[p] < w):
                    can_fire = False
                    break
            if(can_fire):
                fireable.append(t)
        return fireable
    # Fires a specified transition, if possible
    def fire(self, transition):
        if transition not in self.fireable():
            return False
        for p, w in self.arcs_in[transition]:
            if(self.places[p] == 'w'):
                continue
            self.places[p] -= w
        for p, w in self.arcs_out[transition]:
            if(self.places[p] == 'w'):
                continue
            self.places[p] += w
        return True

    # Loads in the Petri Net from a .txt file. Must be of same format as sample.txt
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

    def change_places(self, new_places):
        self.places = copy.deepcopy(new_places)

    class TreeNode:
        def __init__(self, places, parent=None, transition="Initial"):
            self.places = places
            self.parent = parent
            self.children = []
            self.transition = transition
        def add_child(self,child):
            self.children.append(child)
    
    # Returns the coverability tree of the Petri Net. If finite, it is the Reachability Tree
    def coverability_tree(self):
        #grab a copy of this instance of the class
        p = copy.deepcopy(self)
        #queues for BFS
        OPEN = deque()
        CLOSED = []

        root = self.TreeNode(p.places, parent = None)

        OPEN.append(root)
        while(OPEN):
            # grab the next out of the queue
            node = OPEN.popleft()
            # mark it as seen
            CLOSED.append(node.places)

            #update list to node's markings
            p.change_places(node.places)
            
            # process
            # for every fireable transition in this state
            fireable_list = p.fireable()
            for t in fireable_list:
                # update the net to its markings
                p.change_places(node.places)
                # fire the transition
                p.fire(t)
                # grab a copy of its places after the transition
                M = p.places
                # ACCELERATION STEP HERE
                # walk back up until you find root
                parent_itr = node.parent
                while(parent_itr != None):
                    greaterOrEqual = True
                    for place, token in parent_itr.places.items():
                        if M[place] == 'w':
                            continue
                        if M[place] < token:
                            greaterOrEqual = False
                            break
                    if(greaterOrEqual):
                        for place, token in parent_itr.places.items():
                            if M[place] == 'w':
                                continue
                            if M[place] > token:
                                M[place] = 'w'
                    parent_itr = parent_itr.parent

                # ADD TO CHILDREN
                child = self.TreeNode(M,parent=node,transition=t)
                node.children.append(child)
                
                # IF NOT DUPLICATE, ALSO ADD TO OPEN
                if M not in CLOSED:
                    OPEN.append(child)

                CLOSED.append(child.places)
                

        return root
    # recursively prints cov tree
    def print_coverability_tree(self,node, indent=0):
        print(" " * indent + node.transition + " -> " + str(node.places))
        for child in node.children:
            self.print_coverability_tree(child, indent + 4)


# petri_visualizer.py
import json
import threading
import time
import webbrowser
import copy
from copy import deepcopy
from typing import Dict, List
from flask import Flask, jsonify, request, send_file
from flask_socketio import SocketIO, emit
from pyvis.network import Network

class PetriNetVisualizer:
    """
    Web-based Petri net visualizer with coverability tree support.
    """
    def __init__(self, pn, html_filename="petri_net.html", host="127.0.0.1", port=5000):
        self.pn = pn
        self.html_filename = html_filename
        self.host = host
        self.port = port

        self.initial_marking = dict(self.pn.places)
        self.history: List[Dict[str,int]] = [deepcopy(self.pn.places)]
        self.history_index = 0

        self.app = Flask(__name__, static_folder=".")
        self.socketio = SocketIO(self.app, cors_allowed_origins="*", async_mode="eventlet")

        self.net = Network(directed=True, notebook=False, height="100vh", width="50vw")
        self.net.set_options("""
                        "physics": {"enabled": false},
                        "interaction": {"hover": true, "multiselect": false}
                        }
                        """)
        self.place_nodes: Dict[str,str] = {}
        self.transition_nodes: Dict[str,str] = {}
        self.built = False

        self.lock = threading.Lock()

        self._setup_routes()
        self._setup_socket_handlers()

    # -------------------
    # Graph building
    # -------------------
    def build_graph(self):
        self.net = Network(directed=True, notebook=False, height="100vh", width="50vw")

        # Places
        for place, tokens in self.pn.places.items():
            nid = f"p_{place}"
            self.net.add_node(
                nid,
                label=f"{place}\nTokens: {tokens}",
                shape="circle",
                color="#89CFF0",
                size=30 + tokens * 5,
                title=f"Place {place}"
            )
            self.place_nodes[place] = nid
        # Transitions
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
        self.net.set_options("""
        var options = {
          "physics": {"enabled": false},
          "interaction": { "hover": true, "multiselect": false }
        }
        """)
        self.built = True

    def update_nodes_payload(self):
        nodes = []
        for place, tokens in self.pn.places.items():
            nid = self.place_nodes[place]
            nodes.append({
                "id": nid,
                "label": f"{place}\nTokens: {tokens}",
                "color": "#89CFF0",
                "size": 30 + tokens * 5,
                "width": None, "height": None
            })
        for t in self.pn.transitions:
            nid = self.transition_nodes[t]
            nodes.append({
                "id": nid,
                "label": t,
                "color": "#7CFC00" if t in self.pn.fireable() else "#A9A9A9",
                "size": None,
                "width": 110, "height": 60
            })
        return nodes

    # -------------------
    # HTML + frontend
    # -------------------
    def save_html_with_frontend(self):
        self.build_graph()
        self.net.save_graph(self.html_filename)

        with open(self.html_filename, "r", encoding="utf-8") as f:
            html = f.read()

        html = html.replace("height: 500px;", "height: 100vh;").replace("width: 500px;", "width: 50vw;")

        injected_frontend = """
<!-- TOOLBAR -->
<div id="toolbar" style="position:fixed;top:10px;left:10px;z-index:9999;background:rgba(255,255,255,0.9);
padding:8px;border-radius:8px;box-shadow:0 2px 8px rgba(0,0,0,0.15);display:flex;gap:6px;align-items:center;">
  <button onclick="emitStep()">Step</button>
  <button onclick="emitCoverabilityTree()">Coverability Tree</button>
  <button onclick="emitUndo()">Undo</button>
  <button onclick="emitRedo()">Redo</button>
  <button onclick="emitReset()">Reset</button>
  <span id="status" style="margin-left:6px;font-weight:600;"></span>
</div>

<!-- DEADLOCK BANNER -->
<div id="deadlockBanner" style="display:none;position:fixed;top:0;left:0;width:100%;background:red;color:white;
text-align:center;padding:18px;font-size:28px;font-weight:700;z-index:9998;cursor:pointer;">
DEADLOCK — CLICK TO RESET
</div>

<!-- COVERABILITY TREE PANEL -->
<div id="coverabilityTreeContainer" style="position:fixed;top:0;right:0;width:50vw;height:100vh;border-left:2px solid #333;
background:#f8f8f8;overflow:auto;z-index:9997;"></div>

<script src="//cdnjs.cloudflare.com/ajax/libs/socket.io/4.6.1/socket.io.min.js"></script>
<script type="text/javascript">
var socket = io();
function applyNodeUpdate(n){ let u={id:n.id,label:n.label,color:n.color}; if(n.size!==null) u.size=n.size;
if(n.width!==null){u.width=n.width;u.height=n.height;} try{network.body.data.nodes.update(u);}catch(e){console.error(e,u);} }
socket.on("update",function(payload){
    if(payload.nodes) payload.nodes.forEach(n=>applyNodeUpdate(n));
    document.getElementById('deadlockBanner').style.display = payload.deadlocked ? 'block':'none';
    if(payload.message){ document.getElementById('status').innerText=payload.message;
    setTimeout(()=>document.getElementById('status').innerText='',1500);}
});
network.on("click",function(params){
    if(params.nodes && params.nodes.length===1 && params.nodes[0].startsWith("t_")){
        socket.emit("fire",{transition:params.nodes[0].substring(2)});
    }
});
document.getElementById("deadlockBanner").onclick=function(){socket.emit("reset");}
function emitStep(){ var sel=network.getSelectedNodes();
if(sel && sel.length===1 && sel[0].startsWith("t_")) socket.emit("fire",{transition:sel[0].substring(2)});
else socket.emit("step"); }
function emitCoverabilityTree(){socket.emit("show_coverability_tree");}
function emitUndo(){socket.emit("undo");}
function emitRedo(){socket.emit("redo");}
function emitReset(){socket.emit("reset");}
socket.on("coverability_tree_html",function(payload){
    document.getElementById("coverabilityTreeContainer").innerHTML =
    `<iframe src="${payload.html_path}" style="width:100%;height:100%;border:none;"></iframe>`;
});
socket.on("connect",function(){socket.emit("client_ready");});
</script>
"""
        if "</body>" in html:
            html = html.replace("</body>", injected_frontend+"</body>")
        else:
            html += injected_frontend

        with open(self.html_filename,"w",encoding="utf-8") as f:
            f.write(html)

    # -------------------
    # Flask routes
    # -------------------
    def _setup_routes(self):
        @self.app.route("/")
        def index():
            with self.lock:
                self.save_html_with_frontend()
            return send_file(self.html_filename)

        @self.app.route("/coverability_tree.html")
        def coverability_tree_file():
            return send_file("coverability_tree.html")

    # -------------------
    # Socket handlers
    # -------------------
    def _setup_socket_handlers(self):
        @self.socketio.on("client_ready")
        def handle_client_ready():
            with self.lock:
                emit("update",{"nodes":self.update_nodes_payload(),
                               "deadlocked":len(self.pn.fireable())==0})

        @self.socketio.on("fire")
        def handle_fire(message):
            t = message.get("transition")
            if t is None:
                emit("update", {"message":"No transition specified."})
                return
            with self.lock:
                fired = self.pn.fire(t)
                if fired: self._push_history()
                emit("update", {"nodes":self.update_nodes_payload(),
                                "deadlocked":len(self.pn.fireable())==0,
                                "fired":fired,"message":f"Fired {t}"})

        @self.socketio.on("step")
        def handle_step():
            with self.lock:
                avail = self.pn.fireable()
                if not avail:
                    emit("update", {"message":"No transitions fireable."})
                    return
                fired = self.pn.fire(avail[0])
                if fired: self._push_history()
                emit("update", {"nodes":self.update_nodes_payload(),
                                "deadlocked":len(self.pn.fireable())==0,
                                "fired":fired,"message":f"Fired {avail[0]}"})

        @self.socketio.on("show_coverability_tree")
        def handle_show_coverability_tree():
            with self.lock:
                try:
                    root = self.pn.coverability_tree()
                    tree_net = Network(directed=True, notebook=False, height="100vh", width="100%")
                    tree_net.set_options("""
                        var options = {
                        "layout": {
                            "hierarchical": {
                            "enabled": true,
                            "direction": "UD",   
                            "sortMethod": "directed"
                            }
                        },
                         "barnesHut": {
                        "gravitationalConstant": -3000,
                        "centralGravity": 0.0,
                        "springLength": 200,
                        "springConstant": 0.00,
                        "damping": 0.00,
                        "avoidOverlap": 1
                        },
                        "physics": {"enabled": false},
                        "interaction": {"hover": true, "multiselect": false}
                        }
                        """)
                    def add_node_edges(node,parent_id=None):
                        nid=f"ct_{id(node)}"
                        label=f"{node.transition}\n{list(node.places.values())}"
                        color="#FFA500" if node.transition != "Initial" else "#ADD8E6"
                        tree_net.add_node(nid,label=label,shape="box",color=color)
                        if parent_id: tree_net.add_edge(parent_id,nid)
                        for child in node.children: add_node_edges(child,nid)
                    add_node_edges(root)

                    tree_file = "coverability_tree.html"
                    tree_net.save_graph(tree_file)

                   
                    emit("coverability_tree_html",{"html_path":"/coverability_tree.html"})
                except Exception as e:
                    emit("update",{"message":f"Error building coverability tree: {e}"})

        @self.socketio.on("undo")
        def handle_undo():
            with self.lock:
                if self.history_index<=0: emit("update",{"message":"Nothing to undo."}); return
                self.history_index-=1
                self.pn.places=deepcopy(self.history[self.history_index])
                emit("update",{"nodes":self.update_nodes_payload(),
                               "deadlocked":len(self.pn.fireable())==0,"message":"Undone"})

        @self.socketio.on("redo")
        def handle_redo():
            with self.lock:
                if self.history_index>=len(self.history)-1: emit("update",{"message":"Nothing to redo."}); return
                self.history_index+=1
                self.pn.places=deepcopy(self.history[self.history_index])
                emit("update",{"nodes":self.update_nodes_payload(),
                               "deadlocked":len(self.pn.fireable())==0,"message":"Redone"})

        @self.socketio.on("reset")
        def handle_reset():
            with self.lock:
                self.pn.places=dict(self.initial_marking)
                self._push_history()
                emit("update",{"nodes":self.update_nodes_payload(),
                               "deadlocked":len(self.pn.fireable())==0,"message":"Reset to initial marking"})

    # -------------------
    # History helper
    # -------------------
    def _push_history(self):
        self.history=self.history[:self.history_index+1]
        self.history.append(deepcopy(self.pn.places))
        self.history_index+=1
        if len(self.history)>200:
            self.history=self.history[-200:]
            self.history_index=len(self.history)-1

    # -------------------
    # Start server
    # -------------------
    def start(self, open_browser=True):
        with self.lock: self.save_html_with_frontend()
        if open_browser:
            def _open(): time.sleep(0.5); webbrowser.open(f"http://{self.host}:{self.port}/")
            threading.Thread(target=_open,daemon=True).start()
        print(f"Starting server at http://{self.host}:{self.port}/")
        self.socketio.run(self.app,host=self.host,port=self.port,debug=False,use_reloader=False)

# -----------------------
# Minimal example PetriNet for testing
# -----------------------
if __name__ == "__main__":
    
    pn = PetriNet("two_philosophers.txt")
    server = PetriNetVisualizer(pn)
    server.start(open_browser=True)

    # covtree = pn.coverability_tree()
    # pn.print_coverability_tree(covtree)