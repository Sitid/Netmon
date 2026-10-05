"""Configuración central de netmon.

Todas las opciones se leen de variables de entorno con prefijo NETMON_
(los servicios systemd las cargan desde /etc/netmon/netmon.env).
Para desarrollo local alcanza con un archivo .env en el cwd.
"""

from __future__ import annotations

import ipaddress
from datetime import date, datetime
from functools import lru_cache
from zoneinfo import ZoneInfo

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="NETMON_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Base de datos
    db_dsn: str = "postgresql://netmon:netmon@127.0.0.1:5432/netmon"

    # ntopng
    ntopng_url: str = "http://127.0.0.1:3000"
    ntopng_token: str = ""
    ntopng_user: str = ""
    ntopng_pass: str = ""
    ntopng_ifid: int = 0

    # Red
    local_networks: str = "192.168.0.0/16,10.0.0.0/8,172.16.0.0/12"
    gateway_ip: str = "192.168.1.1"
    wan_probes: str = "8.8.8.8"
    internal_dns_ip: str = ""
    capture_iface: str = "eth1"

    # Web
    listen_host: str = "0.0.0.0"
    listen_port: int = 8080
    admin_password: str = "cambiame"
    kiosk_token: str = "cambiame-token"
    secret_key: str = "cambiame-secreto"
    frontend_dir: str = "/opt/netmon/frontend"

    # Intervalos / umbrales
    collect_interval: int = 60
    # zona horaria del negocio: define "hoy", cuotas diarias y días de reporte
    # (el servidor está en US/Eastern; auditoría H18)
    tz: str = "America/Argentina/Buenos_Aires"
    # capacidad por dirección de un enlace (Mbit/s): tope físico para validar deltas
    link_mbps: int = 1000
    flowlog_enabled: bool = True
    # nº de flujos (los de mayor volumen) a registrar por ciclo. Medido 2026-09-29:
    # 6000 ya cubren ~100 % del throughput instantáneo; con 10000 el ciclo del
    # colector llegó a 54-60 s (al límite del minuto); 20000 tarda ~90 s.
    flowlog_top: int = 6000
    live_interval: int = 5
    rt_interval: int = 2           # muestreo del gráfico realtime de interfaz
    ping_interval: int = 10
    ping_count: int = 5
    lat_warn_ms: float = 100.0
    loss_warn_pct: float = 5.0
    degraded_minutes: int = 5

    # Retención (tiers estilo RRD: minuto -> 5 min -> hora)
    retention_min_hours: int = 48
    retention_5min_days: int = 14
    retention_hour_days: int = 90
    ping_retention_days: int = 60

    # AD / Windows (vacío = módulo desactivado)
    ad_winrm_host: str = ""
    ad_winrm_user: str = ""
    ad_winrm_pass: str = ""
    ad_winrm_scheme: str = "http"
    ad_dhcp_server: str = ""
    ad_poll_seconds: int = 300
    user_map_ttl_hours: int = 10

    # SMTP (vacío = sin mails)
    smtp_host: str = ""
    smtp_port: int = 25
    smtp_tls: bool = False
    smtp_user: str = ""
    smtp_pass: str = ""
    smtp_from: str = "netmon@empresa.local"
    smtp_to: str = ""

    # Notificación por webhook (POST JSON; sirve para Telegram/Teams vía relay)
    webhook_url: str = ""

    # Varios
    oui_csv: str = "/opt/netmon/data/oui.csv"
    blocklist_path: str = "/opt/netmon/data/firehol_level1.netset"
    geoip_mmdb: str = "/opt/netmon/data/dbip-country-lite.mmdb"
    reports_dir: str = "/opt/netmon/reports"

    # ---- Helpers derivados -------------------------------------------------
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.tz)

    def today(self) -> date:
        return datetime.now(self.zone()).date()

    def local_nets(self) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
        """Subredes locales parseadas (ignora entradas inválidas con warning implícito)."""
        nets = []
        for chunk in self.local_networks.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            try:
                nets.append(ipaddress.ip_network(chunk, strict=False))
            except ValueError:
                pass
        return nets

    def is_local_ip(self, ip: str) -> bool:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        return any(addr in net for net in self.local_nets())

    def ping_targets(self) -> list[tuple[str, str]]:
        """Lista (clave_semantica, direccion) de targets a monitorear."""
        targets = [("gateway", self.gateway_ip)]
        for probe in self.wan_probes.split(","):
            probe = probe.strip()
            if probe:
                targets.append((f"internet:{probe}", probe))
        if self.internal_dns_ip:
            targets.append(("dns_interno", self.internal_dns_ip))
        return targets


@lru_cache
def get_settings() -> Settings:
    return Settings()
