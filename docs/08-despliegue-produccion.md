# 08 — Despliegue en producción (ESXi + WatchGuard) y lecciones del laboratorio

## Concepto clave: netmon NO se instala en el firewall

El WatchGuard es un appliance cerrado (Fireware): no se le instala software.
netmon corre en un **servidor Linux independiente** (VM en el ESXi) que recibe
una **copia** del tráfico vía el puerto espejo (SPAN) del switch. El firewall
no se modifica ni se toca. La captura es **pasiva y de una sola vía**: netmon
físicamente no puede inyectar tráfico ni cortar la red.

```
   INTERNET
      │
  WatchGuard Firebox ── puerto en el switch ──┐
      │  (rutea VLANs)                         │ el switch COPIA este puerto
   Switch Aruba/Ruckus ─────── SPAN ───────────┘──► port group SPAN-Capture (ESXi)
      │                                                        │ (promiscuo)
   VLANs de usuarios                              VM netmon: NIC2 captura (sin IP)
                                                  NIC1 gestión (con IP) → dashboard
```

## Protocolos / comunicación

| Función | Protocolo / puerto | Notas |
|---|---|---|
| Captura de tráfico | libpcap (pasiva) sobre el SPAN | No habla hacia el firewall; solo escucha |
| Latencia / estado | ICMP (ping) a WatchGuard, 8.8.8.8, DNS | Único "toque" al firewall |
| Usuario AD / DHCP | WinRM TCP 5985 (o 5986 TLS) → DC | Solo lectura (cuenta svc-netmon) |
| Dashboard | HTTP TCP 8080 (HTTPS con reverse proxy) | Restringir a la VLAN de Sistemas |
| NetFlow (opcional) | UDP 2055 desde WatchGuard | No usado: NetFlow no trae SNI |

## Pasos de despliegue

### 1. Exportar la VM validada
Apagar la VM del laboratorio, quitar snapshots y exportar:
```
ovftool "C:\ruta\netmon.vmx" C:\ruta\netmon.ova
```

### 2. Preparar el ESXi (una vez)
- Cablear una NIC física libre del host ESXi al puerto SPAN del switch.
- Crear un **vSwitch dedicado** con ese uplink (sin VMkernel ni otras VMs).
- Crear port group **`SPAN-Capture`** en ese vSwitch. En *Security*:
  **Promiscuous mode = Accept**, MAC changes / Forged transmits = Accept.
  (Es el equivalente del modo "Bridged + promiscuo" de Workstation.)

### 3. Configurar el SPAN en el switch
Espejar el puerto donde está el WatchGuard → ver docs/02 (ArubaOS-Switch,
AOS-CX o Ruckus ICX según el modelo). Verificar con:
```
show monitor        # Aruba/Ruckus
```

### 4. Importar el OVA
Deploy OVF Template → NIC1 al port group de servidores, NIC2 a `SPAN-Capture`.
Subir recursos: 4 vCPU / 8 GB / 60 GB. Disco thin.

### 5. Reconfigurar para producción
Editar `/etc/netmon/netmon.env`:
```ini
NETMON_LOCAL_NETWORKS=<tus VLANs reales, ej. 192.168.10.0/24,192.168.20.0/24>
NETMON_GATEWAY_IP=<IP interna del WatchGuard>
NETMON_INTERNAL_DNS_IP=<IP del DC>
NETMON_CAPTURE_IFACE=<nombre real, en ESXi suele ser ens224>
```
Reflejar las VLANs en `/etc/ntopng/ntopng.conf` (`--local-networks=`) y el
interface (`-i=`). **Regenerar las claves** que se usaron en el lab:
```bash
sudo sed -i "s/^NETMON_ADMIN_PASSWORD=.*/NETMON_ADMIN_PASSWORD=$(openssl rand -base64 12 | tr -d '/+=')/" /etc/netmon/netmon.env
sudo sed -i "s/^NETMON_KIOSK_TOKEN=.*/NETMON_KIOSK_TOKEN=$(openssl rand -hex 24)/" /etc/netmon/netmon.env
sudo sed -i "s/^NETMON_SECRET_KEY=.*/NETMON_SECRET_KEY=$(openssl rand -hex 32)/" /etc/netmon/netmon.env
```

### 6. Ajustar el ifid de ntopng (¡importante!)
Tras reiniciar ntopng con la interfaz nueva, el `ifid` casi seguro cambia:
```bash
sudo systemctl restart ntopng
curl -s "http://127.0.0.1:3000/lua/rest/v2/get/ntopng/interfaces.lua"
```
Anotar el `ifid` de la interfaz de captura y ponerlo en `netmon.env`
(`NETMON_NTOPNG_IFID=`), luego `sudo systemctl restart netmon-collector netmon-api`.

### 7. Integración AD
Crear `svc-netmon` + WinRM → docs/04, luego `systemctl enable --now netmon-adsync`.

### 8. Verificación final
```bash
sudo tcpdump -i <captura> -c 20 vlan          # ¿llega tráfico espejado?
sudo journalctl -u netmon-collector -n 5      # "ciclo ok: N hosts" con N>0
```

## Lecciones aprendidas en el laboratorio (evitar en producción)

Estos son los puntos donde tropezamos instalando en la VM; ya están resueltos
en el código/instalador, pero conviene tenerlos presentes:

1. **ntopng no está en los repos de Debian 12/13**: hay que agregar
   `packages.ntop.org` (el instalador ya lo hace en su versión actual; si se
   corta ahí, ver el paso 1 del install.sh).
2. **Comentarios en línea en `netmon.env`**: systemd los pasa como parte del
   valor y rompen el parseo de pydantic. Los comentarios van en su **propia
   línea** (ya corregido en `.env.example`). Si un servicio queda en
   "activating", revisar esto primero con `sudo journalctl -u netmon-api`.
3. **Permisos de `/home`**: si se corre `install.sh` desde el home del usuario,
   PostgreSQL no puede leer `schema.sql` (home privado). Correr el instalador
   desde una ruta legible (ej. `/opt/src`) o abrir permisos. El instalador
   aplica el schema; si falla con "Permission denied", es esto.
4. **ifid de ntopng ≠ 0**: al capturar en una interfaz, ntopng le asigna un id
   que **no siempre es 0** (en el lab fue 2). Si el dashboard queda vacío pero
   ntopng captura bien, verificar `NETMON_NTOPNG_IFID` contra `interfaces.lua`.
5. **Promiscuo en el hipervisor**: sin "Promiscuous = Accept" en el port group
   (ESXi) o "Bridged" (Workstation), la NIC de captura no ve nada.
6. **Falsos positivos de blocklist en el lab**: pinguear IPs privadas
   inexistentes (bogons) dispara la alerta de reputación. En producción, con
   gateway/DNS reales dentro de `LOCAL_NETWORKS`, no ocurre.
7. **Trabajar por SSH**, no por la consola de la VM: copiar/pegar funciona
   (click derecho), y se evita el problema de las "comillas curvas" al pegar
   comandos con `sed`.

## Endurecimiento recomendado antes de producción

- Restringir el puerto 8080 a la VLAN de Sistemas (regla en el WatchGuard o
  nftables en el servidor).
- HTTPS interno con nginx/caddy delante del 8080 (certificado de la CA interna).
- Backup de la base en el esquema de la empresa:
  `sudo -u postgres pg_dump netmon | gzip > netmon_$(date +%F).sql.gz`
- Política de uso aceptable firmada **antes** de emitir reportes nominales
  (docs/05).
- `apt-mark hold ntopng` si la licencia/maintenance es un tema (ntopng CE no
  requiere licencia, pero deja el aviso).
