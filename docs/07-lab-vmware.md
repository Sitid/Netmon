# 07 — Laboratorio de pruebas en VMware y export a producción

Plan en dos etapas:

- **Etapa A** — VM en VMware Workstation en tu PC: validar instalación, dashboard,
  categorías, alertas y reportes sin tocar nada de la red.
- **Etapa B** — exportar la misma VM como OVF/OVA al ESXi de la empresa y
  conectarla al SPAN real.

> Dato clave de VMware: en una red switcheada normal, una NIC en promiscuo solo
> ve broadcast + su propio tráfico. Pero en Workstation con red **Bridged**, la
> NIC de captura de la VM ve también **el tráfico de tu propia PC** (el bridge
> de VMware se comporta como tap de la NIC física). O sea: tu PC hace de
> "empleado monitoreado" — navegás YouTube y lo ves aparecer clasificado.

---

## Etapa A — VM de laboratorio en Workstation

### A.1 Crear la VM

| Parámetro | Laboratorio | Producción |
|---|---|---|
| SO | Debian 12 o 13 (netinst ISO **amd64**) | igual |
| vCPU / RAM / disco | 2 / 4 GB / 40 GB (thin) | 4 / 8 GB / 60 GB |
| NIC 1 (gestión) | **Bridged** | port group LAN servidores |
| NIC 2 (captura) | **Bridged** | port group SPAN (promiscuo) |

- En **Edit > Virtual Network Editor**: fijá el bridge a tu NIC física real
  (con "Automatic" a veces bridgea al Wi-Fi equivocado).
- Apagada la VM, editá el `.vmx` y poné ambas NICs como vmxnet3 (mejor
  rendimiento y mismos drivers que vas a tener en ESXi):
  ```
  ethernet0.virtualDev = "vmxnet3"
  ethernet1.virtualDev = "vmxnet3"
  ```

### A.2 Instalar Debian

> **amd64 vale para Intel**: es el nombre de la arquitectura x86 de 64 bits
> (la bautizó AMD por haberla creado, pero Intel usa exactamente la misma).
> La ISO amd64 es la correcta para cualquier PC Intel o AMD de 64 bits.
>
> **Debian 12 (bookworm)** hoy es *oldstable*: sigue soportada y es la versión
> contra la que se escribió netmon; la ISO está en
> `cdimage.debian.org/cdimage/archive/latest-oldstable/amd64/iso-cd/`.
> **Debian 13 (trixie)** también funciona: `install.sh` detecta que ntopng ya
> no viene en sus repos y agrega el repositorio oficial de ntop solo.

1. Instalación estándar, **sin entorno de escritorio**; marcá solo
   *SSH server* y *standard system utilities*.
2. Primer arranque: 
   ```bash
   su -
   apt update && apt install -y open-vm-tools sudo curl
   usermod -aG sudo TU_USUARIO
   ```
3. IP fija en la NIC de gestión (`/etc/network/interfaces` o reserva DHCP).
   La NIC de captura queda **sin configurar** (sin IP) — netmon la maneja.
4. Identificá los nombres reales de las interfaces:
   ```bash
   ip -br link      # típico Workstation+vmxnet3: ens33 (gestión), ens34 (captura)
   ```
5. **Snapshot "limpio"** acá: si algo sale mal, volvés en 10 segundos.

### A.3 Copiar netmon e instalar

Desde PowerShell en tu PC (Windows ya trae scp):

```powershell
scp -r "C:\Users\fearo\OneDrive\Desktop\Nueva carpeta\netmon" usuario@IP-DE-LA-VM:/home/usuario/
```

En la VM:

```bash
cd ~/netmon
chmod +x install.sh tools/*.sh
# si algún editor de Windows convirtió finales de línea (error "\r"): 
#   sudo apt install -y dos2unix && find . -name '*.sh' -exec dos2unix {} +
sudo NETMON_CAPTURE_IFACE=ens34 ./install.sh
```

Anotá la clave admin y el token kiosco que imprime al final.

Después editá `/etc/netmon/netmon.env` para el laboratorio:

```ini
NETMON_LOCAL_NETWORKS=192.168.1.0/24        # la subred de TU LAN de prueba
NETMON_GATEWAY_IP=192.168.1.1               # tu router/gateway del lab
NETMON_INTERNAL_DNS_IP=192.168.1.1          # o el DC real si estás en la LAN de la empresa
NETMON_RETENTION_5MIN_DAYS=14
```

Reflejá la misma subred en `/etc/ntopng/ntopng.conf` (`--local-networks=`) y:

```bash
sudo systemctl restart ntopng netmon-collector netmon-pinger netmon-api
```

### A.4 Verificación básica

```bash
# ¿La captura ve tráfico ajeno? (navegá desde tu PC mientras corre)
sudo tcpdump -i ens34 -c 30 -n not host IP-DE-LA-VM

systemctl status netmon-api netmon-collector netmon-pinger --no-pager
journalctl -u netmon-collector -n 20 --no-pager    # esperado: "ciclo ok: N hosts..."
```

Dashboard desde tu PC: `http://IP-DE-LA-VM:8080` → Ingresar con la clave admin.
Kiosco: `http://IP-DE-LA-VM:8080/kiosk?token=EL_TOKEN` (rota Resumen↔Estado cada 30 s).

### A.5 Checklist funcional (qué probar y cómo dispararlo)

| Función | Cómo probarla | Resultado esperado |
|---|---|---|
| Clasificación por categoría | `bash tools/lab_traffic.sh` en la VM, o mirá YouTube/WhatsApp Web desde tu PC | En ~2 min la dona de Resumen y la pestaña de apps muestran Streaming/Social/etc. |
| Top talkers + usuario | navegá fuerte desde tu PC | Tu PC aparece primera en el Top 5 |
| Vista de flujos | Consumo → pestaña "Flujos activos" | Conexiones vivas con app L7; badges LARGA/VOLUMEN en descargas grandes |
| Detalle de host | clic en la IP de tu PC | Histórico, apps, contactos con países, puertos |
| Inventario + dispositivo nuevo | conectá el celular al Wi-Fi de la LAN | Alerta "Dispositivo nuevo" + fila NUEVO en Dispositivos; botón Confirmar |
| Caída de enlace | `sudo iptables -A OUTPUT -d 8.8.8.8 -j DROP` (revertir con `-D`) | En ~30 s KPI "CAÍDA" + alerta crítica; al revertir, alerta de recuperación |
| Degradación | `sudo tc qdisc add dev ens33 root netem delay 150ms` (quitar: `del`) | Alerta "Degradación" tras 5 min sostenidos |
| Cuota diaria | Configuración → regla "Cuota diaria" → 0.2 GB → descargá un ISO de Debian | Alerta de cuota para tu IP (una sola por día) |
| Categoría prohibida (P2P) | bajá un torrent legal (ISO de Debian) con `transmission-cli` | Alerta "Uso de categoría prohibida p2p" |
| Blocklist reputación | `echo "93.184.216.0/24" \| sudo tee -a /opt/netmon/data/firehol_level1.netset` y `curl https://example.com` desde una PC monitoreada | Alerta crítica "IP de mala reputación" (borrá la línea al terminar) |
| Editor de categorías | Config → mover WhatsApp a Productividad | La dona lo recalcula en el próximo ciclo |
| Reportes | dejá la VM juntando datos unas horas → Reportes → PDF | PDF con top y categorías |
| Reconexión del front | `sudo systemctl restart netmon-api` con el dashboard abierto | Banner amarillo "sin conexión… último dato hace X s", reconecta solo |
| AD (si estás en la LAN corporativa) | configurá `NETMON_AD_*` según docs/04 y `systemctl enable --now netmon-adsync` | Columna Usuario poblada, hostnames por DHCP/PTR |

---

## Etapa B — Exportar a ESXi de la empresa

### B.1 Preparar la VM para exportar

```bash
sudo apt clean
history -c
```

- Desconectá la ISO de la unidad de CD y **eliminá los snapshots**
  (VM > Snapshot > Snapshot Manager) — el export no los incluye bien.
- Si la VM va a clonarse más de una vez, además:
  ```bash
  sudo truncate -s0 /etc/machine-id
  sudo rm /etc/ssh/ssh_host_*
  # las keys se regeneran con: sudo dpkg-reconfigure openssh-server (en cada clon)
  ```

### B.2 Exportar

- Con la VM apagada: **File > Export to OVF…** → genera `.ovf` + `.vmdk` + `.mf`.
- Si preferís un solo archivo `.ova`:
  ```
  ovftool "C:\ruta\netmon.vmx" C:\ruta\netmon.ova
  ```
  (`ovftool` viene con Workstation, en `C:\Program Files (x86)\VMware\VMware Workstation\OVFTool\`.)

### B.3 Preparar el ESXi (una sola vez)

1. Cableá una **NIC física libre del host ESXi** al puerto espejo del switch
   (el destino del SPAN de docs/02).
2. vSphere Client → Networking:
   - Crear **vSwitch nuevo** (ej. `vSwitch-SPAN`) con ese uplink físico dedicado.
     Sin VMkernel ni otras VMs ahí.
   - Crear port group **`SPAN-Capture`** en ese vSwitch, y en *Security*:
     **Promiscuous mode = Accept** (imprescindible; sin esto la VM no ve nada),
     MAC address changes / Forged transmits = Accept.
3. El port group de gestión es el normal de la VLAN de servidores.

### B.4 Importar y ajustar

1. vSphere Client → **Deploy OVF Template** → seleccioná el `.ova`/`.ovf`.
   - NIC 1 → port group LAN servidores · NIC 2 → `SPAN-Capture`.
   - Disco: *Thin provision*.
2. Al arrancar, los nombres de interfaz **pueden cambiar** (en ESXi con vmxnet3
   suelen ser `ens192` / `ens224`). Verificá y corregí:
   ```bash
   ip -br link
   sudo nano /etc/netmon/netmon.env        # NETMON_CAPTURE_IFACE=ens224
   sudo nano /etc/ntopng/ntopng.conf       # -i=ens224
   sudo systemctl restart netmon-capture-iface ntopng netmon-collector netmon-api
   ```
3. Subí los recursos a producción: 4 vCPU / 8 GB (Edit Settings).

### B.5 Checklist lab → producción

- [ ] `netmon.env` definitivo: VLANs reales, IP interna del WatchGuard, IP del DC.
- [ ] **Regenerar claves** si las del lab se compartieron (`NETMON_ADMIN_PASSWORD`,
      `NETMON_KIOSK_TOKEN`, `NETMON_SECRET_KEY`) y reiniciar `netmon-api`.
- [ ] Revertir cualquier umbral de prueba (cuota 0.2 GB → valor real) y borrar
      la línea de prueba de la blocklist si la agregaste.
- [ ] SPAN configurado en el switch (docs/02) y `tcpdump -i <captura> -c 20 vlan`
      mostrando tráfico etiquetado de varias IPs.
- [ ] `netmon-adsync` habilitado con la cuenta `svc-netmon` (docs/04).
- [ ] Política de uso comunicada antes de usar reportes nominales (docs/05).
- [ ] Backup: `pg_dump netmon` en el esquema de backups de la empresa.
- [ ] Restringir el puerto 8080 a la VLAN de Sistemas (regla en el WatchGuard).
