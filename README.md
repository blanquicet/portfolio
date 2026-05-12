# Portfolio Tracker

Tracker de portafolio de inversiones personal para colombianos. Self-hosted, operado desde un agente de IA (Claude Code, GitHub Copilot, u otro compatible con skills/slash commands).

Pásale un PDF o screenshot de tu broker al agente — extrae, ingesta y mantiene tu portafolio automáticamente, con costo base FIFO, asignación de lote específico, y reporte de renta para Colombia.

## Requisitos

- Python 3.11+
- Un agente de IA compatible con skills (Claude Code, GitHub Copilot, etc.) con acceso a una API key

## Setup

```bash
# 1. Clonar el repo
git clone https://github.com/blanquicet/portfolio.git
cd portfolio

# 2. Instalar dependencias
pip3 install -r requirements.txt
```

Abre el agente en el directorio `portfolio/` y empieza directo:

- "Agrega estas compras" (adjuntando un PDF, Excel o imagen de tu broker)
- "Registra esta transacción: compré 10 AAPL a $150 el 3 de enero"

La base de datos se crea automáticamente en el primer uso.

## Cómo usarlo

El agente entiende lenguaje natural. No hay comandos exactos que memorizar — dile lo que quieres hacer:

### Registrar compras y ventas

- "Registra esta compra: compré 10 acciones de AAPL a $150 el 3 de enero"
- "Ingesta este PDF de IBKR" (adjuntando el archivo)
- "Procesa este screenshot de Scalable Capital" (adjuntando la imagen)
- "Vendí 5 participaciones de IWDA.L a £80 el 15 de marzo, comisión £5"

El agente extrae los datos, resuelve los tickers automáticamente y los guarda en la base de datos.

### Ver el portafolio actual

- "Muéstrame mi portafolio"
- "¿Cuánto vale mi portafolio hoy?"
- "¿Cuál es mi P&L en MSFT?"
- "Ver posiciones de IBKR"

O directamente desde la terminal:

```bash
# Snapshot con precios en tiempo real
python3 tools/snapshot.py

# Patrimonio histórico al 31-dic de un año (precios históricos de Yahoo Finance)
python3 tools/patrimonio.py 2025
python3 tools/patrimonio.py --as-of 2025-06-30   # fecha arbitraria
python3 tools/patrimonio.py 2025 --csv > patrimonio_2025.csv
```

### Reporte de impuestos / ventas

- "Genera el reporte de renta para 2024"
- "¿Cuánto debo declarar en impuestos por el año 2023?"

O directamente desde la terminal:

```bash
python3 tools/reporte_ventas.py 2025              # tabla por lote FIFO (por defecto)
python3 tools/reporte_ventas.py 2025 --summary    # vista resumida por venta
python3 tools/reporte_ventas.py 2025 --detail     # agrega detalle de lotes en modo summary
python3 tools/reporte_ventas.py 2025 --largo      # solo ventas largo plazo (> 730 días)
python3 tools/reporte_ventas.py 2025 --corto      # solo ventas corto plazo (≤ 730 días)
python3 tools/reporte_ventas.py 2025 --csv > reporte_ventas_2025.csv
```

Hardcodeado para Colombia: Ganancia Ocasional (> 730 días) vs. Renta Ordinaria (≤ 730 días). TRM desde Banco de la República; UVT desde DIAN (actualizado anualmente en el script).

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
