#!/usr/bin/env bash
# Exporta alertas de Wazuh (indexer) a NDJSON + CSV para analizar aparte
# (p. ej. alimentar a otro agente para un desglose).
#
# Uso:
#   deploy/wazuh/export-alerts.sh "<query lucene>" <horas> <max> <salida_base>
# Ejemplos:
#   deploy/wazuh/export-alerts.sh 'rule.level:>=5' 24 2000 /tmp/soc
#   deploy/wazuh/export-alerts.sh 'rule.groups:windows' 24 1000 /tmp/ad
#   deploy/wazuh/export-alerts.sh 'rule.id:(100100 OR 100101)' 168 500 /tmp/reput
#
# Se autentica con el certificado admin del indexer (no usa contraseña).
set -euo pipefail

QUERY="${1:-rule.level:>=5}"
HOURS="${2:-24}"
SIZE="${3:-2000}"
OUT="${4:-/tmp/soc-export}"
CONT=single-node-wazuh.indexer-1
C=/usr/share/wazuh-indexer/certs/admin.pem
K=/usr/share/wazuh-indexer/certs/admin-key.pem

read -r -d '' BODY <<JSON || true
{"size": $SIZE, "sort": [{"@timestamp": "desc"}],
 "query": {"bool": {"must": [
   {"query_string": {"analyze_wildcard": true, "query": "$QUERY"}},
   {"range": {"@timestamp": {"gte": "now-${HOURS}h"}}}]}}}
JSON

docker exec -i "$CONT" curl -s -k --cert "$C" --key "$K" \
  "https://localhost:9200/wazuh-alerts-*/_search" \
  -H 'Content-Type: application/json' -d "$BODY" > "${OUT}.raw.json"

python3 - "$OUT" <<'PY'
import json, sys, csv

out = sys.argv[1]
raw = json.load(open(out + ".raw.json"))
hits = raw.get("hits", {}).get("hits", [])

def flat(o, p=""):
    r = {}
    if isinstance(o, dict):
        for k, v in o.items():
            r.update(flat(v, f"{p}.{k}" if p else k))
    elif isinstance(o, list):
        r[p] = ", ".join(str(x) for x in o)
    else:
        r[p] = o
    return r

rows = [flat(h.get("_source", {})) for h in hits]

# NDJSON completo (una alerta por linea)
with open(out + ".ndjson", "w") as f:
    for h in hits:
        f.write(json.dumps(h.get("_source", {}), ensure_ascii=False) + "\n")

# CSV con las columnas mas utiles para un desglose
cols = ["@timestamp", "rule.level", "rule.id", "rule.description", "rule.groups",
        "agent.name", "data.src_ip", "data.dest_ip", "data.dest_port",
        "data.alert.signature", "data.alert.category",
        "data.win.system.eventID", "data.win.eventdata.targetUserName",
        "data.win.eventdata.ipAddress"]
with open(out + ".csv", "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow({c: r.get(c, "") for c in cols})

print(f"alertas exportadas: {len(rows)}")
print(f"  {out}.ndjson  (completo, 1 alerta/linea)")
print(f"  {out}.csv     (columnas clave)")
PY
