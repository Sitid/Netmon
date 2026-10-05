# 02 — Configuración de port mirroring (SPAN)

## Qué espejar

**Origen**: el puerto del switch core donde está conectado el **WatchGuard
Firebox** (el trunk con todas las VLANs). Con el ruteo en el firewall, por ese
puerto pasa todo el tráfico a internet y el inter-VLAN.

**Destino**: un puerto libre del mismo switch, conectado a la **segunda NIC
del servidor netmon** (la que queda sin IP).

Reglas de oro:
- El puerto destino queda dedicado: no ponerle VLAN de datos ni usarlo para gestión.
- Espejar **ambos sentidos** (`both`/tx+rx).
- Si el core es stack, origen y destino conviene que estén en la misma unidad.

En los ejemplos: **puerto 1 = al Firebox (origen)**, **puerto 24 = al servidor
(destino)**. Ajustá a tu numeración real.

---

## HPE / Aruba — ArubaOS-Switch / ProCurve (2530, 2540, 2920, 2930F…)

```
configure
# sesión de mirror 1 con destino el puerto 24
mirror 1 port 24
# origen: todo el tráfico del puerto 1 (el trunk al Firebox), ambos sentidos
interface 1 monitor all both mirror 1
write memory
```

Verificar: `show monitor` (debe listar el puerto 1 como monitored y el 24 como
mirror destination).

En equipos muy viejos con sintaxis legacy:

```
configure
mirror-port 24
interface 1 monitor
write memory
```

## HPE Aruba — AOS-CX (6000, 6100, 6200, 6300…)

```
configure
mirror session 1
  destination interface 1/1/24
  source interface 1/1/1 both
  enable
exit
write memory
```

Verificar: `show mirror 1`.

## Ruckus ICX — FastIron (ICX 7150, 7250, 7450…)

```
configure terminal
mirror-port ethernet 1/1/24
interface ethernet 1/1/1
 monitor ethernet 1/1/24 both
exit
write memory
```

Verificar: `show monitor` / `show mirror`.

> Si el trunk al Firebox sale de un switch Ruckus y no del core Aruba, el
> mirror se configura en ese Ruckus y el servidor se conecta ahí.

---

## Verificación desde el servidor

Con el SPAN activo y la NIC de captura conectada:

```bash
# ¿Llegan paquetes con tags de VLAN y MACs variados?
sudo tcpdump -i eth1 -e -c 20 vlan
# ¿Se ve tráfico de varias IPs internas?
sudo tcpdump -i eth1 -c 50 -n | awk '{print $3}' | sort -u
```

Si `tcpdump` muestra tráfico pero ntopng no, revisar que
`netmon-capture-iface.service` esté activo (desactiva offloads y sube MTU).

---

## Apéndice: NetFlow del WatchGuard (opcional, no requerido)

Fireware ≥ 12.9 exporta NetFlow v9: **Fireware Web UI → System → NetFlow**,
collector = IP de gestión del servidor netmon, puerto 2055. Sirve como
respaldo si algún día el SPAN queda fuera de servicio, o para contrastar
volúmenes. No lo integramos al dashboard porque NetFlow no transporta SNI y
no puede alimentar la clasificación por categorías; si se quisiera consumir,
`nfdump/nfcapd` lo recibe sin tocar nada de netmon.
