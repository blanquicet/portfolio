# Portfolio Tracker

Tracker de portafolio de inversiones personal para colombianos. Self-hosted, operado desde un agente de IA (Claude Code, GitHub Copilot, u otro compatible con skills/slash commands).

Pásale un PDF o screenshot de tu broker al agente — extrae, ingesta y mantiene tu portafolio automáticamente, con costo base FIFO, asignación de lote específico, y reporte de renta para Colombia.

## Requisitos

- Python 3.11+
- Un agente de IA compatible con slash commands (Claude Code, GitHub Copilot, etc.) con acceso a una API key

## Setup

```bash
# 1. Clonar el repo
git clone https://github.com/blanquicet/portfolio.git
cd portfolio

# 2. Instalar dependencias
pip3 install -r requirements.txt

# 3. Inicializar la base de datos
# Abre el agente en este directorio y ejecuta:
/setup
```

## Comandos

Estos son slash commands — compatibles con Claude Code, GitHub Copilot y otros agentes que soporten skills.

| Comando | Qué hace |
|---------|----------|
| `/setup` | Crea o migra la base de datos |
| `/ingest` | Ingesta transacciones desde un PDF o screenshot |
| `/snapshot` | Muestra posiciones actuales con precios live y P&L |
| `/snapshot ibkr` | Snapshot filtrado por broker |
| `/tax 2024` | Reporte de renta para el año fiscal 2024 |

## Reporte de Renta

El reporte está **hardcodeado para Colombia**:
- **Ganancia Ocasional**: activos mantenidos más de 730 días
- **Renta Ordinaria**: activos mantenidos 730 días o menos
- TRM (tasa de cambio) desde Banco de la República
- UVT desde DIAN (actualizado anualmente en `tools/tax_report.py`)

El sistema clasifica cada venta — cuánto pagar lo determina la ley vigente.

## Resolución de Tickers

Cuando se ingesta un nuevo instrumento, el sistema intenta resolver automáticamente su ticker de Yahoo Finance a partir del ISIN. Si no puede (ambigüedad o falla de búsqueda), el agente te pregunta:

1. En qué bolsa opera el instrumento (ej. LSE, NASDAQ, XETRA)
2. O directamente el ticker de Yahoo Finance (búscalo en finance.yahoo.com)

Los tickers resueltos se guardan en la base de datos local — no te volverá a preguntar.

## Fallback Manual TRM

Si la API del Banco de la República no está disponible, el agente te indicará descargar las tasas manualmente:

1. Ve a: https://suameca.banrep.gov.co/estadisticas-economicas/informacionSerie/1/tasa_cambio_peso_colombiano_trm_dolar_usd
2. Cambia a vista "Tabla"
3. Selecciona las fechas de interés y descarga
4. Ejecuta: `python3 tools/load_trm.py <archivo_descargado.txt>`

## Privacidad

Tus datos de portafolio quedan locales. `portfolio.db` está en `.gitignore` y nunca se sube al repositorio. Solo código genérico y schemas SQL van en git.
