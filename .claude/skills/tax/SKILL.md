---
name: tax
description: "Reporte de renta Colombia — úsame cuando el usuario pida el reporte de impuestos, renta, o declaración de renta para un año fiscal."
---

# Reporte de Renta Colombia

Ejecuta el script de tax report para el año solicitado.

## Pasos

1. Determina el año fiscal (si el usuario no lo especifica, usa el año anterior al actual).

2. Ejecuta:
```bash
python3 tools/tax_report.py <año>
```

3. Muestra el output completo al usuario sin resumir ni truncar.
