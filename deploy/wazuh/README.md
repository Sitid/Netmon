# Wazuh — configuración del SOC (netmon)

Instalación desde cero (wazuh-docker 4.9.0 + parche + clave de enrolamiento +
reglas): ver `docker/README.md`.

## Retención de índices (ISM)
Política `netmon_retencion_90d`: borra los índices `wazuh-alerts-4.x-*` y
`wazuh-archives-4.x-*` a los 90 días. Se aplica sola a los índices nuevos
(ism_template) y ya está adjunta a los existentes.

Recrear / reaplicar (desde el host, credenciales del indexer en single-node/.env):
```
C="curl -sk -u <user>:<pass> -H Content-Type:application/json"
$C -X PUT  https://127.0.0.1:9200/_plugins/_ism/policies/netmon_retencion_90d -d @netmon-ism-retencion-90d.json
$C -X POST "https://127.0.0.1:9200/_plugins/_ism/add/wazuh-alerts-4.x-*,wazuh-archives-4.x-*" -d '{"policy_id":"netmon_retencion_90d"}'
```
Ver estado:  `$C https://127.0.0.1:9200/_plugins/_ism/explain/wazuh-alerts-4.x-*`

## Reglas de Suricata silenciadas
Ver deploy/suricata/disable.conf.
