from flask import Flask, jsonify, request, send_file
from pyvis.network import Network
import threading
import webbrowser
import os
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



# petri_visualizer.py
import threading
import webbrowser
import time
import os
from flask import Flask, jsonify, request, send_file
from pyvis.network import Network

class PetriNetVisualizer:
    def __init__(self, pn, html_filename="petri_net.html", host="127.0.0.1", port=5000):
        """
        pn: an object that has:
            - pn.places: dict(place_name -> int tokens)
            - pn.transitions: list of transition names
            - pn.arcs_in: { transition: [ (place, weight), ... ] }
            - pn.arcs_out: { transition: [ (place, weight), ... ] }
            - pn.fireable(): returns list of fireable transition names
            - pn.fire(t): fires transition t (returns True if fired)
        Option A (reset tokens only) is implemented: initial marking saved and restored.
        """
        self.pn = pn
        self.html_filename = html_filename
        self.host = host
        self.port = port

        # Save initial marking for Option A (reset tokens only)
        self.initial_marking = dict(self.pn.places)

        # pyvis network and bookkeeping
        self.net = Network(directed=True, notebook=False, height="100vh", width="100vw")
        self.place_nodes = {}
        self.transition_nodes = {}
        self.built = False

        # thread-safety
        self.lock = threading.Lock()

        # Flask app (created here, routes bound in start_server)
        self.app = Flask(__name__)

        # Setup Flask routes
        self._setup_routes()

    # -------------------
    # Graph building
    # -------------------
    def build_graph(self):
        """
        Build nodes/edges once. For rectangles, width/height must be used (size ignored).
        """
        if self.built:
            return

        # clear any previous nodes/edges (in case)
        self.net = Network(directed=True, notebook=False, height="100vh", width="100vw")

        # Places
        for place, tokens in self.pn.places.items():
            node_id = f"p_{place}"
            label = f"{place}\nTokens: {tokens}"
            self.net.add_node(
                node_id,
                label=label,
                shape='circle',
                color='#89CFF0',
                size=30 + tokens * 5,
                title=f"Place {place}"
            )
            self.place_nodes[place] = node_id

        # Transitions (boxes) - use width & height for rectangles
        for t in self.pn.transitions:
            node_id = f"t_{t}"
            color = '#7CFC00' if t in self.pn.fireable() else '#A9A9A9'
            # larger width/height for easier clicking
            self.net.add_node(
                node_id,
                label=t,
                shape='box',
                color=color,
                width=100,
                height=50,
                title="Click to fire"
            )
            self.transition_nodes[t] = node_id

        # Edges
        for t, arcs in self.pn.arcs_in.items():
            for place, w in arcs:
                self.net.add_edge(self.place_nodes[place], self.transition_nodes[t], label=str(w), smooth='curvedCW')
        for t, arcs in self.pn.arcs_out.items():
            for place, w in arcs:
                self.net.add_edge(self.transition_nodes[t], self.place_nodes[place], label=str(w), smooth='curvedCCW')

        # Set some client options for nicer layout/physics
        options = """
        var options = {
          "physics": {
            "stabilization": false
          },
          "interaction": { "hover": true }
        }
        """
        self.net.set_options(options)

        self.built = True

    def update_graph_data_for_js(self):
        """
        Produce a list of node updates for the JS side:
        returns list of [id, label, color, size_or_width, height_if_box]
        size_or_width used as 'size' for circles or 'width' for boxes. We will return width,height for boxes.
        """
        nodes_data = []
        # places (circles)
        for place, tokens in self.pn.places.items():
            node_id = self.place_nodes[place]
            label = f"{place}\\nTokens: {tokens}"
            size = 30 + tokens * 5
            nodes_data.append({
                "id": node_id,
                "label": label,
                "color": '#89CFF0',
                "size": size,
                "width": None,
                "height": None
            })
        # transitions (boxes)
        for t in self.pn.transitions:
            node_id = self.transition_nodes[t]
            color = '#7CFC00' if t in self.pn.fireable() else '#A9A9A9'
            nodes_data.append({
                "id": node_id,
                "label": t,
                "color": color,
                "size": None,
                "width": 100,
                "height": 50
            })
        return nodes_data

    # -------------------
    # HTML save + JS injection
    # -------------------
    def save_html_with_js(self):
        """
        Save the pyvis graph to HTML, then append custom JS for:
          - click handler to POST /fire_transition
          - deadlock banner (if deadlocked) that POSTs /reset and reloads
          - node update logic on response
        """
        self.build_graph()
        # generate baseline HTML
        self.net.save_graph(self.html_filename)

        # read the file
        with open(self.html_filename, "r", encoding="utf-8") as f:
            html = f.read()

        # ensure the network container uses full viewport (id is usually "mynetwork")
        # replace common fixed sizes, if present
        html = html.replace("height: 500px;", "height: 100vh;")
        html = html.replace("width: 500px;", "width: 100vw;")

        # Is it a deadlock?
        deadlocked = (len(self.pn.fireable()) == 0)

        # JS: click handler, fetch, and update nodes
        injected_js = f"""
<script type="text/javascript">
// Helper to update a node (handles circles and boxes)
function updateNodeFromData(data) {{
    // data: {{id, label, color, size, width, height}}
    let update = {{ id: data.id, label: data.label, color: data.color }};
    if(data.size !== null) {{
        update.size = data.size;
    }} else if(data.width !== null) {{
        // use physics options to update width/height on box nodes
        update.width = data.width;
        update.height = data.height;
    }}
    try {{
        network.body.data.nodes.update(update);
    }} catch (e) {{
        console.error("updateNode error:", e, update);
    }}
}}

// when clicking nodes, attempt to fire transitions
network.on("click", function(params) {{
    if(params.nodes && params.nodes.length > 0) {{
        var nodeId = params.nodes[0];
        if(nodeId.startsWith("t_")) {{
            fetch("/fire_transition", {{
                method: "POST",
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify({{ transition: nodeId.substring(2) }})
            }}).then(r => r.json()).then(payload => {{
                if(payload.error) {{
                    console.warn(payload.error);
                    return;
                }}
                // update nodes returned
                payload.nodes.forEach(function(n) {{
                    updateNodeFromData(n);
                }});
                // if server says deadlocked, show banner (or reload to get banner)
                if(payload.deadlocked) {{
                    // reload to let server inject banner (simpler)
                    location.reload();
                }}
            }});
        }}
    }}
}});
</script>
"""

        # Deadlock banner HTML + JS (banner calls /reset and reload)
        banner_html = ""
        if deadlocked:
            banner_html = """
<div id="deadlockBanner" style="
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
    z-index: 9999;
    cursor: pointer;
">
    DEADLOCK — CLICK TO RESET
</div>
<script>
document.getElementById("deadlockBanner").onclick = function() {
    fetch("/reset", {method: "POST"}).then(r => r.json()).then(_ => location.reload());
};
</script>
"""
        # Insert banner at top of <body>, and append our injected JS before </body>
        if "<body>" in html:
            html = html.replace("<body>", "<body>" + banner_html, 1)
        html = html.replace("</body>", injected_js + "</body>", 1)

        # write back html
        with open(self.html_filename, "w", encoding="utf-8") as f:
            f.write(html)

    # -------------------
    # Flask server
    # -------------------
    def _setup_routes(self):
        @self.app.route("/")
        def index():
            # Serve the saved HTML file
            return send_file(self.html_filename)

        @self.app.route("/fire_transition", methods=["POST"])
        def fire_transition():
            payload = request.get_json() or {}
            t = payload.get("transition")
            if t is None:
                return jsonify({"error": "no transition provided"}), 400

            with self.lock:
                fired = False
                try:
                    fired = self.pn.fire(t)
                except Exception as e:
                    return jsonify({"error": f"error firing transition: {e}"}), 500

                # after firing (or attempting), prepare nodes update payload
                nodes_data = self.update_graph_data_for_js()
                deadlocked = (len(self.pn.fireable()) == 0)

            # Convert nodes_data into list expected by JS
            js_nodes = []
            for n in nodes_data:
                js_nodes.append({
                    "id": n["id"],
                    "label": n["label"],
                    "color": n["color"],
                    "size": n["size"],
                    "width": n["width"],
                    "height": n["height"]
                })
            return jsonify({"nodes": js_nodes, "fired": fired, "deadlocked": deadlocked})

        @self.app.route("/reset", methods=["POST"])
        def reset_endpoint():
            with self.lock:
                # Option A: reset tokens only
                self.pn.places = dict(self.initial_marking)
                # update graph data (server will regenerate HTML on next save)
                nodes_data = self.update_graph_data_for_js()
                deadlocked = (len(self.pn.fireable()) == 0)
            js_nodes = []
            for n in nodes_data:
                js_nodes.append({
                    "id": n["id"],
                    "label": n["label"],
                    "color": n["color"],
                    "size": n["size"],
                    "width": n["width"],
                    "height": n["height"]
                })
            return jsonify({"status": "ok", "nodes": js_nodes, "deadlocked": deadlocked})

    # -------------------
    # Public API
    # -------------------
    def start_server_thread(self):
        """
        Start the flask server in a background thread. Use use_reloader=False.
        """
        def run_app():
            # disable Flask startup messages in debug situations
            self.app.run(host=self.host, port=self.port, debug=False, use_reloader=False)

        thread = threading.Thread(target=run_app, daemon=True)
        thread.start()
        # small sleep to ensure server is up before we open the browser
        time.sleep(0.3)

    def start(self, open_browser=True):
        """
        Start the visualizer:
          - build graph
          - save html with injected JS/banner
          - start flask server
          - open browser
        """
        # build + write initial HTML (banner injected if initial state deadlocked)
        with self.lock:
            self.save_html_with_js()

        # start server
        self.start_server_thread()

        # open browser to the page
        url = f"http://{self.host}:{self.port}/"
        if open_browser:
            webbrowser.open(url)
        print(f"PetriNet visualizer running at {url} — HTML file: {os.path.abspath(self.html_filename)}")

    def reset(self):
        """
        Programmatic reset (same as clicking the banner).
        Option A: reset token counts only.
        """
        with self.lock:
            self.pn.places = dict(self.initial_marking)
            # regenerate the HTML on disk (so reloading will show banner updated)
            self.save_html_with_js()

# -------------------
# Example usage
# -------------------
if __name__ == "__main__":


    pn = PetriNet()
    viz = PetriNetVisualizer(pn)
    viz.start(open_browser=True)



