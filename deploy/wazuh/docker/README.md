# Wazuh (Docker single-node) — instalación desde cero

Wazuh corre con el proyecto oficial `wazuh-docker` **v4.9.0**, sin forks. Lo único
propio son 3 cambios, en `wazuh-docker-4.9.0-netmon.patch` (no contiene claves):

| Archivo | Cambio | Para qué |
|---|---|---|
| `single-node/docker-compose.yml` | monta `/var/log/suricata` en el manager (`:ro`) | que el manager vea el `eve.json` de Suricata |
| `single-node/config/wazuh_cluster/wazuh_manager.conf` | `<localfile>` json de `/var/log/suricata/eve.json` | ingesta de alertas de Suricata |
| idem | `<use_password>yes</use_password>` | enrolamiento de agentes con clave |

(El parche también quita `version: '3.7'` del compose, obsoleto en Compose v2.)

## Pasos

Requisitos: Docker + Compose v2, `vm.max_map_count=262144`, Suricata escribiendo
en `/var/log/suricata/eve.json` (ver `deploy/suricata/`).

```bash
sudo sysctl -w vm.max_map_count=262144
echo vm.max_map_count=262144 | sudo tee /etc/sysctl.d/99-wazuh.conf

git clone -b v4.9.0 --depth 1 https://github.com/wazuh/wazuh-docker.git ~/wazuh-docker
cd ~/wazuh-docker
git apply /ruta/a/netmon/deploy/wazuh/docker/wazuh-docker-4.9.0-netmon.patch

cd single-node
docker compose -f generate-indexer-certs.yml run --rm generator   # certificados propios
docker compose up -d
```

Cambiar las contraseñas por defecto del indexer/dashboard siguiendo la guía
oficial de wazuh-docker ("Change the password of Wazuh users") **antes** de
exponer el dashboard.

### Clave de enrolamiento de agentes

```bash
PASS=$(openssl rand -hex 16)
docker exec single-node-wazuh.manager-1 sh -c "echo '$PASS' > /var/ossec/etc/authd.pass \
  && chown wazuh:wazuh /var/ossec/etc/authd.pass && chmod 640 /var/ossec/etc/authd.pass"
docker restart single-node-wazuh.manager-1
```

Guardar la clave en un gestor de contraseñas; se usa al registrar agentes
(ver `../agent-dc/README.md`).

### Reglas locales

```bash
docker cp ../local_rules.xml single-node-wazuh.manager-1:/var/ossec/etc/rules/local_rules.xml
docker exec single-node-wazuh.manager-1 chown wazuh:wazuh /var/ossec/etc/rules/local_rules.xml
docker restart single-node-wazuh.manager-1
```

### Retención y Grafana

- Retención 90 días: `../README.md` (política ISM).
- Grafana: `deploy/grafana/README.md` (debe unirse a la red `single-node_default`).

## Verificar

```bash
docker compose ps                                   # 3 contenedores Up
docker exec single-node-wazuh.manager-1 grep -c suricata /var/ossec/etc/ossec.conf
```

En el dashboard (`https://IP-del-servidor`): *Threat Hunting* → filtrar
`rule.groups: suricata` para ver las alertas de Suricata.
