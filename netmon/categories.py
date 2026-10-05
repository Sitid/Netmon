"""Mapeo de protocolos nDPI -> categorías de negocio.

Taxonomía (validada con el usuario): streaming, social, productividad,
sistema, p2p, desconocido.

"desconocido" incluye el tráfico TLS/HTTP/QUIC genérico donde nDPI identificó
el transporte pero no la aplicación: es la semántica correcta (no sabemos qué
es), y evita inflar categorías con supuestos.

El mapeo por defecto vive acá; los overrides por aplicación se editan desde
Configuración (tabla category_map) y tienen prioridad. La comparación es
case-insensitive; nDPI emite nombres compuestos como "TLS.YouTube" y se evalúa
el segmento más específico primero.

Para listar los nombres exactos que emite tu versión de nDPI:
    ndpiReader -H   (o ntopng -> Settings -> Applications)
"""

from __future__ import annotations

STREAMING = {
    "youtube", "youtubeupload", "netflix", "amazonvideo", "primevideo",
    "disney", "disneyplus", "twitch", "spotify", "deezer", "soundcloud",
    "applemusic", "appletv", "tidal", "hbo", "dazn", "vimeo", "dailymotion",
    "iqiyi", "pluto", "starplus", "youtubemusic", "rtmp", "avastreaming", "webradio",
}

SOCIAL = {
    "facebook", "instagram", "twitter", "x.com", "tiktok", "snapchat",
    "pinterest", "reddit", "linkedin", "tumblr", "threads", "whatsapp",
    "whatsappfiles", "telegram", "signal", "discord", "badoo", "tinder",
}

PRODUCTIVIDAD = {
    # suite / correo
    "microsoft365", "office365", "outlook", "exchange", "activesync",
    "sharepoint", "ms_onedrive", "onedrive", "smtp", "smtps", "imap",
    "imaps", "pop3", "pop3s", "gmail", "googledocs", "googledrive",
    # reuniones / mensajería laboral
    "teams", "msteams", "skype", "skype_teamscall", "zoom", "googlemeet",
    "meet", "webex", "slack",
    # negocio / bases / acceso remoto
    "sap", "oracle", "mssql", "mysql", "postgresql", "mongodb", "rdp",
    "vnc", "anydesk", "teamviewer", "citrix", "github", "gitlab",
    # nube de archivos / vpn de trabajo
    "dropbox", "wetransfer", "mega", "openvpn", "wireguard", "ipsec", "vpn",
}

SISTEMA = {
    # actualizaciones y telemetría de plataforma
    "windowsupdate", "windows_update", "microsoft", "azure", "apple",
    "applestore", "icloud", "ubuntuone", "ubuntu", "debian", "nvidia",
    "avast", "antivirus", "ocsp", "crashlytics",
    # descargas grandes de plataformas de juego (bulk download; mover si molesta)
    "steam", "epicgames", "playstation", "xbox", "nintendo",
    # infraestructura de red
    "dns", "mdns", "llmnr", "netbios", "ssdp", "dhcp", "dhcpv6", "ntp",
    "snmp", "syslog", "ldap", "kerberos", "smb", "smbv23", "smbv1",
    "icmp", "icmpv6", "igmp", "stun",
}

P2P = {
    "bittorrent", "bt", "edonkey", "gnutella", "soulseek", "tor", "i2p",
    "zeronet", "ipfs",
}

CAMARAS = {
    "hikvision", "rtsp", "onvif", "dahua", "rtsp_control", "hikvision_stream",
}

# Nombre visible y orden estable para dashboard/reportes
CATEGORY_LABELS = {
    "streaming": "Streaming",
    "camaras": "Cámaras",
    "social": "Social",
    "productividad": "Productividad",
    "sistema": "Sistema",
    "p2p": "P2P",
    "desconocido": "Desconocido",
}
CATEGORY_ORDER = ["streaming", "camaras", "social", "productividad", "sistema", "p2p", "desconocido"]

_RULES: list[tuple[str, set[str]]] = [
    ("camaras", CAMARAS),
    ("streaming", STREAMING),
    ("social", SOCIAL),
    ("productividad", PRODUCTIVIDAD),
    ("sistema", SISTEMA),
    ("p2p", P2P),
]

_cache: dict[str, str] = {}


def _default_category(key: str) -> str:
    """Clasificación por defecto (sin overrides), con cache."""
    if key in _cache:
        return _cache[key]
    result = "desconocido"
    # el sufijo de un nombre compuesto es la app concreta ("TLS.YouTube")
    for seg in reversed(key.split(".")):
        for cat, names in _RULES:
            if seg in names or any(seg.startswith(n) for n in names if len(n) > 3):
                result = cat
                break
        if result != "desconocido":
            break
    _cache[key] = result
    return result


def categorize(proto_name: str, overrides: dict[str, str] | None = None) -> str:
    """Categoría de negocio para un protocolo nDPI.

    `overrides` (app en minúsculas -> categoría) viene de la tabla category_map;
    se prueba contra el nombre completo y contra el segmento más específico.
    """
    key = proto_name.lower()
    if overrides:
        if key in overrides:
            return overrides[key]
        last = key.rsplit(".", 1)[-1]
        if last in overrides:
            return overrides[last]
    return _default_category(key)
