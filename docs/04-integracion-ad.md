# 04 — Integración con Active Directory / Windows

Tres integraciones, todas de **solo lectura** y sin agentes en los servidores
Windows. Cada una es independiente: activá las que quieras.

| Integración | Aporta | Requiere |
|---|---|---|
| PTR contra DNS del dominio | IP → hostname | Nada (solo la IP del DNS en `netmon.env`) |
| Leases DHCP de Windows Server | hostname autoritativo + MAC | Cuenta de servicio + WinRM |
| Eventos Kerberos 4768 del DC | IP → **usuario AD logueado** | Cuenta de servicio en *Event Log Readers* + WinRM |

## 1. Cuenta de servicio (en el DC)

```powershell
# Crear la cuenta (sin privilegios administrativos)
New-ADUser -Name "svc-netmon" -SamAccountName svc-netmon `
    -AccountPassword (Read-Host -AsSecureString "Clave") `
    -Enabled $true -PasswordNeverExpires $true `
    -Description "Lectura de eventos/DHCP para monitoreo de red (netmon)"

# Permiso de lectura del log de seguridad
Add-ADGroupMember -Identity "Event Log Readers" -Members svc-netmon

# Permiso de lectura del DHCP (grupo local del servidor DHCP)
Add-ADGroupMember -Identity "DHCP Users" -Members svc-netmon
```

> Principio de mínimo privilegio: esta cuenta NO es admin de dominio, no puede
> escribir nada. Solo lee eventos y leases.

## 2. Habilitar WinRM para esa cuenta

En el DC (y en el server DHCP si es otro equipo):

```powershell
Enable-PSRemoting -Force        # normalmente ya está activo en servers
```

Además, los miembros de *Event Log Readers* necesitan permiso de conexión
remota WinRM. Lo más simple es agregar la cuenta al grupo local
**Remote Management Users** del DC:

```powershell
Add-LocalGroupMember -Group "Remote Management Users" -Member "DOMINIO\svc-netmon"
```

Firewall: permitir TCP **5985** (o 5986 para HTTPS, recomendado) **solo desde
la IP de gestión del servidor netmon** — se puede hacer con una regla de
firewall de Windows con ámbito por IP remota, o vía GPO.

Para WinRM sobre HTTPS (recomendado en producción): emitir un certificado de
la CA interna al DC, `winrm quickconfig -transport:https`, y en `netmon.env`
poner `NETMON_AD_WINRM_SCHEME=https`.

## 3. Configurar netmon

En `/etc/netmon/netmon.env`:

```ini
NETMON_INTERNAL_DNS_IP=192.168.10.5
NETMON_AD_WINRM_HOST=dc01.empresa.local
NETMON_AD_WINRM_USER=EMPRESA\svc-netmon
NETMON_AD_WINRM_PASS=la-clave
NETMON_AD_WINRM_SCHEME=http          # https si configuraste el certificado
NETMON_AD_DHCP_SERVER=               # vacío si el DHCP corre en el mismo DC
```

Activar y verificar:

```bash
systemctl enable --now netmon-adsync
journalctl -u netmon-adsync -f
# esperable: "PTR: x/y resueltos", "DHCP: n leases", "Kerberos: n eventos"
```

## Cómo funciona el mapeo IP → usuario

Cuando un usuario inicia sesión o desbloquea su PC, el equipo pide un ticket
Kerberos al DC; eso genera el **evento 4768** en el log Security del DC, que
incluye la cuenta y la IP de origen. netmon lee esos eventos cada 5 minutos y
mantiene una tabla IP→usuario con vencimiento (`NETMON_USER_MAP_TTL_HOURS`,
default 10 h ≈ una jornada). Se descartan cuentas de máquina (`PC-01$`) y
`krbtgt`.

Limitaciones a tener presentes:

- **PCs compartidas**: se muestra el último usuario que pidió ticket, no todos
  los logueados.
- **Equipos solo Azure AD** (no unidos al dominio on-prem): no generan 4768;
  quedan con hostname pero sin usuario. En un híbrido con *hybrid join*
  (lo típico) sí funcionan.
- El evento 4768 se audita por defecto en los DCs. Si alguien lo deshabilitó:
  GPO *Default Domain Controllers Policy* → Advanced Audit → Account Logon →
  **Audit Kerberos Authentication Service: Success**.

## Verificación de zonas PTR (para la resolución por DNS)

En el DC: consola DNS → *Reverse Lookup Zones*. Si no existe la zona inversa
de alguna VLAN (ej. `10.168.192.in-addr.arpa`), crearla y habilitar
*dynamic updates* seguros; los clientes Windows registran su PTR solos (o los
registra el DHCP si está configurado para hacerlo). Mientras tanto, netmon
resuelve nombres igual vía leases DHCP, que es la fuente de mayor prioridad.
