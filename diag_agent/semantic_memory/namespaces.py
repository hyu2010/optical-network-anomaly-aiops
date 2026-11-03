from rdflib import Namespace

ONM = Namespace("https://optical-aiops.example/ns/rfc9940#")
OPTX = Namespace("https://optical-aiops.example/ns/optical#")
DX = Namespace("https://optical-aiops.example/ns/diagnosis#")
KB = Namespace("https://optical-aiops.example/kb#")
G = Namespace("https://optical-aiops.example/graph/")
OBS = Namespace("https://optical-aiops.example/obs/")
CASE = Namespace("urn:x-diag-case:")

PREFIXES = {"onm": ONM, "optx": OPTX, "dx": DX, "kb": KB, "g": G, "obs": OBS}


def net_ns(topology_id: str) -> Namespace:
    return Namespace(f"https://optical-aiops.example/net/{topology_id}/")


def sparql_prefixes() -> str:
    lines = [f"PREFIX {p}: <{ns}>" for p, ns in PREFIXES.items()]
    lines += ["PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>",
              "PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>",
              "PREFIX owl: <http://www.w3.org/2002/07/owl#>",
              "PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>"]
    return "\n".join(lines) + "\n"
