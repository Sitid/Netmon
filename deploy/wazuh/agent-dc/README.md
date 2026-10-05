# Agente Wazuh en el Controlador de Dominio (AD/DC)

Procedimiento para instalar y vincular un agente **Wazuh 4.9.0** en un DC
(Windows Server) contra el manager que corre en el Debian `netmon-srv`.

> **Servidor validado GxP.** Cambio autorizado el 2026-09-30. El agente queda
> configurado en modo **sólo lectura/reporte**: no ejecuta acciones sobre el DC
> (active-response deshabilitado). Reversión = desinstalar el agente (paso 7).

---

## 0. Datos del entorno (verificados)

| Qué | Valor |
|---|---|
| Manager (host) | `netmon-srv` = **10.10.11.59** |
| Puerto eventos | **1514/tcp** |
| Puerto enrolamiento | **1515/tcp** |
| Versión manager | wazuh-manager **4.9.0** (Docker single-node) → usar agente **4.9.x** |
| Config del agente | [`ossec-agent-dc.conf`](ossec-agent-dc.conf) |

El manager y el DC están en el mismo segmento (10.10.10/11.x), no hace falta ruteo.

---

## 1. Pre-requisitos y red

1. Desde el DC tiene que llegar al manager por 1514 y 1515:
   ```powershell
   Test-NetConnection 10.10.11.59 -Port 1514
   Test-NetConnection 10.10.11.59 -Port 1515
   ```
   Ambos deben dar `TcpTestSucceeded : True`.
2. Si hay firewall entre medio, habilitar salida del DC → 10.10.11.59:1514,1515/tcp.
3. Hora sincronizada (el DC ya es fuente NTP del dominio; el Debian debe estar en hora).

---

## 2. Endurecer el enrolamiento (YA APLICADO — 2026-09-30)

> **Estado: HECHO.** El manager exige contraseña de enrolamiento
> (`use_password=yes`). La clave está en `/var/ossec/etc/authd.pass` dentro del
> contenedor del manager. El cambio se hizo en el archivo del host
> `wazuh-docker/single-node/config/wazuh_cluster/wazuh_manager.conf` (durable ante
> recreación) y en el contenedor. Backup del host en `wazuh_manager.conf.bak-*`.
>
> **Consecuencia para futuros agentes:** cualquier alta nueva DEBE presentar esa
> clave, con `agent-auth.exe -m 10.10.11.59 -p 1515 -P "<clave>"` o pasando
> `WAZUH_REGISTRATION_PASSWORD="<clave>"` al `msiexec`. Recuperar la clave con:
> ```bash
> docker exec single-node-wazuh.manager-1 cat /var/ossec/etc/authd.pass
> ```

Procedimiento con el que se aplicó (referencia). En una LAN corporativa conviene
exigir una clave compartida para que ningún equipo no autorizado se registre solo.

En el **Debian**:

```bash
# 1) definir una clave de enrolamiento
PASS="$(openssl rand -base64 24)"
echo "$PASS" | sudo tee /var/lib/docker/volumes/single-node_wazuh_etc/_data/authd.pass >/dev/null 2>&1 \
  || docker exec single-node-wazuh.manager-1 sh -c "echo '$PASS' > /var/ossec/etc/authd.pass && chown wazuh:wazuh /var/ossec/etc/authd.pass && chmod 640 /var/ossec/etc/authd.pass"
echo "Guardá esta clave: $PASS"

# 2) activar use_password en el ossec.conf del manager
docker exec single-node-wazuh.manager-1 sed -i \
  's#<use_password>no</use_password>#<use_password>yes</use_password>#' \
  /var/ossec/etc/ossec.conf

# 3) reiniciar el manager
docker restart single-node-wazuh.manager-1
```

Si activás esto, en el paso 4 hay que pasarle la misma clave al agente
(`-P "<clave>"` en el registro, o `authd.pass` en el DC).

> Si preferís no tocarlo ahora, saltá este paso; el procedimiento funciona igual.

---

## 3. Instalar el agente en el DC

1. Bajar el MSI 4.9.0 (en una máquina con internet, no en el DC si el DC no tiene salida):
   `https://packages.wazuh.com/4.x/windows/wazuh-agent-4.9.0-1.msi`
2. Copiarlo al DC e instalar en **silencioso**, ya apuntando al manager:
   ```powershell
   msiexec.exe /i wazuh-agent-4.9.0-1.msi /q `
     WAZUH_MANAGER="10.10.11.59" `
     WAZUH_REGISTRATION_SERVER="10.10.11.59" `
     WAZUH_AGENT_NAME="$env:COMPUTERNAME"
   ```
   Si activaste la clave de enrolamiento (paso 2), agregar:
   `WAZUH_REGISTRATION_PASSWORD="<clave>"`

Esto instala en `C:\Program Files (x86)\ossec-agent\`.

> **Hallazgo real del despliegue (2026-09-30).** En Windows Server 2012 R2 el MSI
> **no escribió** la dirección del manager en `ossec.conf` (quedó `0.0.0.0`) y el
> servicio arrancaba y se caía con:
> `ERROR: (4112): Invalid server address found: '0.0.0.0'` / `(1215): No client configured`.
> Además el enrolamiento automático no se disparó. Solución aplicada:
>
> 1. Enrolar a mano (crea `client.keys`):
>    ```powershell
>    cd "C:\Program Files (x86)\ossec-agent"
>    .\agent-auth.exe -m 10.10.11.59 -p 1515
>    ```
>    (con clave de enrolamiento: agregar `-P "<clave>"`). Debe decir `Valid key received`.
> 2. Corregir la dirección en `ossec.conf`:
>    ```powershell
>    $cfg = "C:\Program Files (x86)\ossec-agent\ossec.conf"
>    (Get-Content $cfg) -replace '<address>0\.0\.0\.0</address>','<address>10.10.11.59</address>' | Set-Content $cfg -Encoding ASCII
>    Start-Service WazuhSvc
>    ```
>    (Este paso se vuelve innecesario al aplicar la config del paso 4, que ya trae la IP.)

---

## 4. Aplicar la configuración recomendada

1. Copiar [`ossec-agent-dc.conf`](ossec-agent-dc.conf) sobre
   `C:\Program Files (x86)\ossec-agent\ossec.conf` (reemplaza el default).
   Si el DC no puede recibir el archivo, se puede escribir el contenido con un
   here-string de PowerShell (`@' ... '@ | Set-Content $cfg -Encoding ASCII`);
   el `'@` de cierre debe quedar pegado al margen izquierdo o PowerShell falla.
   Conviene hacer backup antes: `Copy-Item $cfg "$cfg.bak" -Force`.
2. Reiniciar el servicio:
   ```powershell
   Restart-Service -Name WazuhSvc
   ```
3. Verificar registro y conexión en el log del agente:
   ```powershell
   Get-Content "C:\Program Files (x86)\ossec-agent\ossec.log" -Tail 30
   ```
   Buscá `Valid key received` y `Connected to the server`.

Qué hace esta config (resumen):
- **Eventos filtrados** del canal *Security*: logins fallidos (4625), bloqueos
  (4740), Kerberos/NTLM (4771/4776), alta/baja/cambios de cuentas (4720-4726, 4738),
  cambios en grupos privilegiados (4728/4732/4756 y sus bajas), privilegios
  especiales (4672) y borrado del log de auditoría (1102).
- **System**: sólo errores/críticos y arranque/parada/servicios nuevos.
- **Inventario** cada 6 h → detección de vulnerabilidades.
- **FIM** cada 12 h, sin realtime, sin SYSVOL ni NTDS.
- **Active-response OFF**: el agente no ejecuta nada en el DC.

> Para que 4625/4740/47xx se generen, el DC necesita las políticas de auditoría
> avanzada activas (Logon, Account Management, Account Logon). En un DC de dominio
> ya suelen estarlo; si algún EventID no aparece, revisar `Advanced Audit Policy`.

> **Nota SO.** El DC verificado corre **Windows Server 2012 R2** (fuera de soporte).
> La detección de vulnerabilidades marcará el propio SO como crítico/EOL; es
> esperado, no un falso positivo.

---

## 5. Reglas y detección (lado manager)

No hay que escribir reglas a mano: Wazuh 4.9 ya trae el ruleset de Windows.

- **Decoders/reglas Windows eventchannel** (`0575-win-*`, `18100+`): clasifican
  automáticamente los EventID de arriba (fuerza bruta, lockout, cambios de admin,
  clear log, etc.) con su `rule.level`. Eso es lo que alimenta el tablero de
  triage de Grafana que ya está provisionado.
- **Detección de vulnerabilidades**: en 4.9 corre en el manager/indexer a partir
  del inventario (`syscollector`) del agente. Ya viene habilitada en la imagen;
  las vulnerabilidades del DC aparecen en el dashboard de Wazuh
  (Vulnerability Detection) al completarse el primer inventario.
- **Retención**: las alertas del DC caen bajo la política ISM de 90 días ya
  aplicada (`../netmon-ism-retencion-90d.json`).

Si más adelante querés reglas propias (p. ej. subir el nivel de 4728 en horario
no laboral), van en `local_rules.xml` del manager — se documenta aparte.

---

## 6. Verificación desde el Debian (manager)

```bash
# el DC debe figurar Active
docker exec single-node-wazuh.manager-1 /var/ossec/bin/agent_control -l

# ver eventos entrando del agente (por su id o nombre)
docker exec single-node-wazuh.manager-1 tail -f /var/ossec/logs/archives/archives.log | grep -i "<HOSTNAME_DC>"
```

También en el dashboard de Wazuh (https://10.10.11.59): *Agents* → el DC en verde,
y en el tablero de triage SOC de Grafana empiezan a aparecer sus alertas.

---

## 7. Reversión (rollback)

En el **DC**:
```powershell
Stop-Service WazuhSvc
msiexec.exe /x wazuh-agent-4.9.0-1.msi /q
```
En el **manager** (limpiar el registro del agente):
```bash
docker exec single-node-wazuh.manager-1 /var/ossec/bin/manage_agents -r <ID_DEL_AGENTE>
```
Con esto el DC queda exactamente como antes (sólo se instaló/desinstaló un servicio).

---

## 8. Consumo esperado en el DC

- RAM: ~30–90 MB en reposo; picos cortos durante inventario/FIM.
- CPU: <1 % en reposo; FIM cada 12 h es el único costo notable (acotado, sin realtime).
- Red: saliente a 10.10.11.59:1514, comprimido; volumen acotado por el filtro de EventID.
- Disco: ~100–150 MB de instalación + cola local si el manager no responde.
