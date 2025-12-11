import re

class PetriNet:
    def __init__(self):
        self.places = {}          # { place: tokens }
        self.transitions = []     # [ transition ]
        self.arcs_in = {}         # { transition: [(place, weight), ...] }
        self.arcs_out = {}        # { transition: [(place, weight), ...] }

    def __repr__(self):
        return (
            f"Places: {self.places}\n"
            f"Transitions: {self.transitions}\n"
            f"Fireable Transitions: {self.fireable()}\n"
            f"Arcs In: {self.arcs_in}\n"
            f"Arcs Out: {self.arcs_out}\n"
             
        )

    def __is_fireable__(self, transition):
        """Return True if all input places have enough tokens for this transition."""
        inputs = self.arcs_in.get(transition, [])
        for place, weight in inputs:
            if self.places.get(place, 0) < weight:
                return False
        return True

    def fireable(self):
        """Generates a list of all fireable transitions at the current state"""
        fireable_list = []
        for t in self.transitions:
            if(self.__is_fireable__(t)):
                fireable_list.append(t)
        return fireable_list

    def fire_transitions(self, transition):

        # If the transition cannot be fired, return false
        if(not self.__is_fireable__(transition)):
            return False
        
        # Consume the input tokens
        for place, weight in self.arcs_in.get(transition, []):
            self.places[place] -= weight

        # Produce the output tokens
        for place, weight in self.arcs_out.get(transition, []):
            self.places[place] = self.places.get(place,0) + weight        
            


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



from pyvis.network import Network
import dash
from dash import html, dcc, Input, Output, State
import dash_cytoscape as cyto

class PetriNetVisualizer:
    def __init__(self, petri_net):
        self.pn = petri_net
        self.net = Network(directed=True, notebook=False)
        self.transition_nodes = {}
        self.place_nodes = {}

    def build_graph(self):
        # Add places
        for place, tokens in self.pn.places.items():
            label = f"{place}\nTokens: {tokens}"
            node_id = f"p_{place}"
            self.net.add_node(node_id, label=label, shape='circle', color='#89CFF0', size=30 + tokens*5)
            self.place_nodes[place] = node_id

        # Add transitions
        for t in self.pn.transitions:
            label = t
            node_id = f"t_{t}"
            color = '#A9A9A9'
            if t in self.pn.fireable():  # optional: highlight fireable transitions
                color = '#7CFC00'
            self.net.add_node(node_id, label=label, shape='box', color=color, size=25)
            self.transition_nodes[t] = node_id

        # Add arcs (inputs)
        for t, arcs in self.pn.arcs_in.items():
            for place, weight in arcs:
                self.net.add_edge(self.place_nodes[place], self.transition_nodes[t], arrows='to', label=str(weight))

        # Add arcs (outputs)
        for t, arcs in self.pn.arcs_out.items():
            for place, weight in arcs:
                self.net.add_edge(self.transition_nodes[t], self.place_nodes[place], arrows='to', label=str(weight))

    def show(self, filename="petri_net.html"):
        self.build_graph()
        self.net.show(filename, notebook=False)
        print(f"Interactive Petri Net visual saved as {filename}")


if __name__ == "__main__":
    pn = PetriNet()
    pn.load_petri_net("petri.txt")
    print(pn)

    viz = PetriNetVisualizer(pn)
    viz.show()