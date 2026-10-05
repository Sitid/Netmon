# 05 — Privacidad, marco legal y política de uso aceptable

> Esto es una guía técnica-organizativa, no asesoramiento legal. Antes de
> poner el sistema en producción, validar el texto final con el asesor legal
> de la empresa (en una farmacéutica, también con Calidad/Compliance).

## Qué monitorea el sistema (y qué NO)

**Sí — metadatos de red:**
- Volúmenes de tráfico por equipo (bytes subidos/bajados, tasas).
- Categoría de aplicación deducida del **SNI** del handshake TLS y de
  consultas DNS (ej.: "este equipo movió 2 GB clasificados como YouTube").
- Inventario L2: MAC, fabricante, hostname, primera/última vez visto.
- Usuario de AD asociado a un equipo (del evento de logon del propio dominio).
- Latencia y disponibilidad de enlaces.

**No — contenido:**
- No se desencripta TLS ni se hace inspección de contenido (no hay MITM).
- No se almacenan URLs completas, cuerpos, mails, chats ni archivos.
- No se capturan credenciales; los paquetes no se guardan en disco (ntopng
  procesa en memoria y descarta; netmon persiste solo contadores agregados).

**Minimización aplicada por diseño:**
- Retención acotada: detalle por minuto 48 h, agregados horarios 90 días
  (configurable). Pasado el plazo se borra automáticamente.
- Acceso restringido: clave de administrador para datos con usuario; el modo
  kiosco muestra lo mismo que ve Sistemas.
- El SNI/hostname de destino se usa solo para *clasificar* en categorías; el
  dashboard y los reportes muestran categorías, no el detalle de sitios.

## Marco legal (Argentina)

- **Ley 25.326 (Protección de Datos Personales)**: los registros asociables a
  una persona (IP+usuario AD) son datos personales → exigen finalidad
  explícita, proporcionalidad, seguridad y acceso del titular. La finalidad
  acá es *gestión de capacidad y seguridad de la red corporativa*, que es
  legítima y proporcional si se monitorean metadatos y no contenido.
- **Contrato de trabajo / jurisprudencia laboral**: el empleador puede
  controlar herramientas de trabajo, pero el control debe ser **conocido por
  el empleado de antemano**, general (no dirigido a una persona sin causa) y
  no invasivo de comunicaciones privadas. La notificación previa y por
  escrito es la pieza clave para que un reporte sea utilizable en un proceso
  disciplinario.
- Si hubiera filiales en otros países, revisar equivalentes (GDPR en UE exige
  además registro de actividad de tratamiento y evaluación de impacto).

**Recomendaciones concretas:**
1. Publicar la política (abajo) y hacerla firmar (alta de empleado + refuerzo
   anual). Guardar constancia.
2. Los reportes a gerencia: entregar por equipo/área; incluir usuario puntual
   solo cuando haya una justificación registrada (investigación de incidente,
   pedido de RRHH).
3. Designar responsables de acceso al dashboard admin (Sistemas) y dejar
   escrito quién puede pedir reportes nominales y con qué causa.
4. No extender el sistema a inspección de contenido (proxy MITM) sin una
   revisión legal separada: es otro nivel de invasividad.

## Plantilla de comunicación / política de uso aceptable (adaptar)

---

### Política de uso aceptable de la red corporativa — [EMPRESA]

**1. Alcance.** Aplica a todo dispositivo conectado a la red de [EMPRESA],
propio o personal, cableado o Wi-Fi.

**2. Uso permitido.** La red es una herramienta de trabajo. Se admite un uso
personal razonable y ocasional siempre que no degrade el servicio, no
comprometa la seguridad ni infrinja la ley o esta política.

**3. Usos prohibidos.** Descarga/distribución de material ilegal, compartir
credenciales, conectar equipos de red no autorizados (routers, APs), eludir
los controles de seguridad, y el uso intensivo recreativo sostenido
(streaming/juegos) en horario laboral que afecte el ancho de banda común.

**4. Monitoreo — qué se registra.** Por razones de seguridad y gestión de
capacidad, [EMPRESA] registra **metadatos técnicos** del tráfico de red:
volúmenes por equipo, categoría de aplicación (ej. "streaming", "correo"),
identificación del dispositivo (dirección de red, nombre de equipo, usuario
de dominio) y métricas de calidad del enlace. **No se accede al contenido**
de las comunicaciones: no se leen correos, mensajes, archivos ni páginas
visitadas.

**5. Retención y acceso.** Los registros se conservan hasta [90] días y solo
accede a ellos el personal de Sistemas autorizado. Los reportes a la
dirección se realizan de forma agregada; se individualizan únicamente ante
incidentes de seguridad o requerimientos formales.

**6. Dispositivos nuevos.** Todo dispositivo detectado que no esté
inventariado será verificado por Sistemas y puede ser bloqueado.

**7. Consecuencias.** El incumplimiento puede derivar en medidas
disciplinarias conforme al reglamento interno y la legislación vigente.

Firma del empleado: ______________  Fecha: ______

---

## Nota para el contexto farmacéutico

El sistema no toca datos de producción ni sistemas GxP (no intercepta, solo
observa una copia del tráfico), por lo que en general queda fuera del alcance
de validación CSV; conviene igual registrarlo en el inventario de sistemas de
IT con su evaluación de impacto ("no GxP, datos de infraestructura") para que
quede prolijo ante una auditoría.
