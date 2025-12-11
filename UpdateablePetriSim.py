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


# --- PyVis Visualizer ---
class PetriNetVisualizer:
    def __init__(self, pn: PetriNet):
        self.pn = pn
        self.net = Network(directed=True, notebook=False)
        self.place_nodes = {}
        self.transition_nodes = {}
        self.built = False

    def build_graph(self):
        if self.built:
            return

        # Places
        for place, tokens in self.pn.places.items():
            node_id = f"p_{place}"
            label = f"{place}\nTokens: {tokens}"
            self.net.add_node(node_id, label=label, shape='circle', color='#89CFF0', size=30 + tokens*5)
            self.place_nodes[place] = node_id

        # Transitions
        for t in self.pn.transitions:
            node_id = f"t_{t}"
            color = '#7CFC00' if t in self.pn.fireable() else '#A9A9A9'
            self.net.add_node(
                node_id,
                label=t,
                shape='box',
                color=color,
                width = 40,
                height = 40,
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

        self.built = True

    def update_graph(self):
        # Update places
        for place, tokens in self.pn.places.items():
            node_id = self.place_nodes[place]
            self.net.get_node(node_id)['label'] = f"{place}\nTokens: {tokens}"
            self.net.get_node(node_id)['size'] = 30 + tokens*5

        # Update transitions
        for t in self.pn.transitions:
            node_id = self.transition_nodes[t]
            color = '#7CFC00' if t in self.pn.fireable() else '#A9A9A9'
            self.net.get_node(node_id)['color'] = color
            self.net.get_node(node_id)['width'] = 40
            self.net.get_node(node_id)['height'] = 40

    def save_html(self, filename="petri_net.html"):
        self.build_graph()
        self.update_graph()
        # Inject custom JS for clickable transitions
        js = """
        <script type="text/javascript">
        network.on("click", function(params) {
            if(params.nodes.length > 0) {
                var nodeId = params.nodes[0];
                if(nodeId.startsWith("t_")){
                    fetch("/fire_transition", {
                        method: "POST",
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({transition: nodeId.substring(2)})
                    }).then(response => response.json()).then(data => {
                        // update node labels and colors
                        for(const [id, label, color, size] of data.nodes){
                            network.body.data.nodes.update({id:id,label:label,color:color,size:size});
                        }
                    });
                }
            }
        });
        </script>
        """
        self.net.save_graph(filename)
        with open(filename, "a") as f:
            f.write(js)

# --- Flask Server ---
app = Flask(__name__)
pn = PetriNet()
viz = PetriNetVisualizer(pn)

@app.route("/")
def index():
    return send_file("petri_net.html")

@app.route("/fire_transition", methods=["POST"])
def fire_transition():
    data = request.get_json()
    transition = data.get("transition")
    pn.fire(transition)
    viz.update_graph()
    # Send back updated node info: [id, label, color, size]
    nodes_data = []
    for place, tokens in pn.places.items():
        node_id = viz.place_nodes[place]
        label = f"{place}\nTokens: {tokens}"
        size = 30 + tokens*5
        nodes_data.append([node_id, label, '#89CFF0', size])
    for t in pn.transitions:
        node_id = viz.transition_nodes[t]
        color = '#7CFC00' if t in pn.fireable() else '#A9A9A9'
        nodes_data.append([node_id, t, color, 25])
    return jsonify({"nodes": nodes_data})

# --- Run Server ---
def open_browser():
    webbrowser.open("http://127.0.0.1:5000")

if __name__ == "__main__":
    viz.save_html("petri_net.html")
    threading.Timer(1, open_browser).start()
    app.run(debug=True)
