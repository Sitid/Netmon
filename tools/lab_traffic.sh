#!/usr/bin/env bash
# Genera tráfico de prueba clasificable por SNI, para validar la detección de
# categorías en el laboratorio (correr DENTRO de la VM o en una PC que la NIC
# de captura pueda ver). No hace falta que las páginas carguen enteras: al
# clasificar por SNI, con el handshake TLS ya alcanza.
#
# Uso: ./lab_traffic.sh [vueltas]     (default: 20)

LOOPS="${1:-20}"

URLS=(
  # streaming
  https://www.youtube.com https://i.ytimg.com https://www.netflix.com
  https://open.spotify.com
  # social
  https://www.instagram.com https://www.facebook.com https://web.whatsapp.com
  https://www.reddit.com
  # productividad
  https://outlook.office365.com https://teams.microsoft.com https://github.com
  # sistema
  https://update.microsoft.com https://swcdn.apple.com
  # web genérica (debe caer en "desconocido")
  https://www.wikipedia.org https://www.mercadolibre.com.ar
)

for i in $(seq 1 "$LOOPS"); do
  for u in "${URLS[@]}"; do
    curl -sL --max-time 8 -o /dev/null "$u" 2>/dev/null || true
  done
  echo "vuelta $i/$LOOPS — esperá ~2 min y mirá Resumen > Tráfico por categoría"
  sleep 2
done
