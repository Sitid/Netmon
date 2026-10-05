"""Nombres amigables para dominios (SNI) — 'pv-cdn.net' -> 'Prime Video'.

El colector guarda el dominio registrable (ver reg_domain); acá lo traducimos
a un nombre legible para reportes y dashboard. Editá SITE_NAMES para sumar más.
"""

from __future__ import annotations

SITE_NAMES: dict[str, str] = {
    # streaming video
    "pv-cdn.net": "Prime Video", "amazonvideo.com": "Prime Video",
    "aiv-cdn.net": "Prime Video", "aiv-delivery.net": "Prime Video",
    "nflxvideo.net": "Netflix", "netflix.com": "Netflix", "nflximg.net": "Netflix",
    "googlevideo.com": "YouTube", "youtube.com": "YouTube", "ytimg.com": "YouTube",
    "youtube-nocookie.com": "YouTube",
    "dssott.com": "Disney+", "bamgrid.com": "Disney+", "disneyplus.com": "Disney+",
    "disney-plus.net": "Disney+",
    "cvattv.com.ar": "Flow (Cablevisión)",
    "twitch.tv": "Twitch", "ttvnw.net": "Twitch",
    # música
    "spotifycdn.com": "Spotify", "spotify.com": "Spotify", "scdn.co": "Spotify",
    # redes sociales / mensajería
    "fbcdn.net": "Facebook", "facebook.com": "Facebook", "fbsbx.com": "Facebook",
    "instagram.com": "Instagram", "cdninstagram.com": "Instagram",
    "tiktokcdn.com": "TikTok", "tiktokv.com": "TikTok", "tiktok.com": "TikTok",
    "ibytedtos.com": "TikTok", "byteoversea.com": "TikTok", "tiktokcdn-us.com": "TikTok",
    "whatsapp.net": "WhatsApp", "whatsapp.com": "WhatsApp",
    "twimg.com": "X (Twitter)", "twitter.com": "X (Twitter)", "x.com": "X (Twitter)", "t.co": "X (Twitter)",
    "telegram.org": "Telegram", "telegram.me": "Telegram", "t.me": "Telegram",
    "licdn.com": "LinkedIn", "linkedin.com": "LinkedIn",
    # Microsoft / Google / Apple / nubes
    "live.com": "Microsoft", "office.com": "Microsoft 365", "office.net": "Microsoft 365",
    "office365.com": "Microsoft 365", "microsoft.com": "Microsoft", "static.microsoft": "Microsoft",
    "cloud.microsoft": "Microsoft 365", "sharepoint.com": "SharePoint", "sfx.ms": "Microsoft",
    "windows.com": "Windows", "windows.net": "Microsoft Azure", "windowsupdate.com": "Windows Update",
    "msftconnecttest.com": "Microsoft", "msedge.net": "Microsoft", "outlook.com": "Outlook",
    "microsoftonline.com": "Microsoft 365", "skype.com": "Skype",
    "google.com": "Google", "googleapis.com": "Google", "gstatic.com": "Google",
    "gvt1.com": "Google", "gvt2.com": "Google", "googleusercontent.com": "Google",
    "google-analytics.com": "Google", "googletagmanager.com": "Google",
    "apple.com": "Apple", "icloud.com": "Apple iCloud", "cdn-apple.com": "Apple",
    "mzstatic.com": "Apple", "aaplimg.com": "Apple",
    "amazonaws.com": "Amazon AWS", "cloudfront.net": "Amazon CloudFront", "a2z.com": "Amazon",
    "myqcloud.com": "Tencent Cloud",
    # trabajo / varios
    "adobe.com": "Adobe", "adobe.io": "Adobe",
    "claude.ai": "Claude (Anthropic)", "anthropic.com": "Claude (Anthropic)",
    "github.com": "GitHub", "githubusercontent.com": "GitHub",
    "mercadolibre.com": "MercadoLibre", "mercadolibre.com.ar": "MercadoLibre", "mlstatic.com": "MercadoLibre",
    "kaspersky-labs.com": "Kaspersky", "kaspersky.com": "Kaspersky",
    "hikvision.com": "Hikvision", "svcmot.com": "Motorola",
    "samsungcloud.com": "Samsung", "samsungosp.com": "Samsung", "smartthings.com": "Samsung",
    # CDNs genéricos (no dicen la app, se aclara que es CDN)
    "akamaized.net": "Akamai (CDN)", "akamai.net": "Akamai (CDN)", "akamaihd.net": "Akamai (CDN)",
    "cloudflare.com": "Cloudflare (CDN)", "fastly.net": "Fastly (CDN)",
    # adultos (para reportes/RRHH)
    "xvideos-cdn.com": "XVideos (adulto)", "xvideos.com": "XVideos (adulto)",
    "phncdn.com": "Pornhub (adulto)", "pornhub.com": "Pornhub (adulto)",
}


def friendly_site(domain: str) -> str:
    """Nombre amigable del dominio, o el dominio si no está en el mapa."""
    if not domain:
        return domain
    return SITE_NAMES.get(domain.lower(), domain)


def site_domains(name: str) -> list[str]:
    """Dominios que se muestran con ese nombre amigable (o el propio dominio)."""
    doms = [d for d, n in SITE_NAMES.items() if n == name]
    return doms or [name.lower()]
