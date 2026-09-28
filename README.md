# TCO Bolivia — bases de cálculo del Tipo de Cambio Oficial

Descarga cada día las operaciones de compra de dólares que el Banco Central de Bolivia publica como base de cálculo del TCO, las guarda en este repositorio y las muestra en un dashboard estático.

## Cómo funciona

`scripts/fetch_tco.py` consulta la página del BCB (`bcb_tco_publico_detalle_historico.php`), lee la lista de fechas de corte publicadas, descarga el CSV de cada fecha que aún no está en `data/raw/` y reconstruye las tablas derivadas:

| Archivo | Contenido |
|---|---|
| `data/raw/AAAA-MM-DD.csv` | CSV original del BCB, sin tocar |
| `data/operaciones.csv` | una fila por fecha de corte × banco × tipo de cambio: número de operaciones y monto en USD |
| `data/diario.csv` | una fila por fecha de corte × banco: monto, TC promedio, TC mediana, TC publicado, USD bajo/al/sobre el TCO y TCO sin el banco |
| `data/total.csv` | una fila por fecha de corte: método vigente, TCO publicado, promedio y mediana recalculados, colchones |
| `data/verificacion.csv` | TCO recalculado con la fórmula vigente vs. TCO publicado |
| `data/tco.json` | datos compactos para el dashboard |
| `data/tco.db` | SQLite con las tablas `operaciones`, `diario`, `total` y `verificacion` |

El workflow `.github/workflows/actualizar.yml` corre el script a las 01:30 UTC (21:30 de La Paz) y de nuevo a las 13:00 UTC, y hace commit de los datos nuevos. También se puede lanzar a mano desde la pestaña Actions.

`index.html` es el dashboard: lee `data/tco.json` y se publica con GitHub Pages desde la raíz de la rama `main`.

## Correr localmente

```
python3 scripts/fetch_tco.py                  # descarga lo que falte y reconstruye
python3 scripts/fetch_tco.py --sin-descarga   # solo reconstruye desde raw/
python3 -m http.server 8000                   # abrir http://localhost:8000
```

Solo requiere Python 3.10 o superior, sin dependencias externas.

## Metodología del TCO

Hasta el corte del 24/09/2026 el TCO era el promedio ponderado por monto de las compras de dólares de los bancos (RD 88/2026). Desde el corte del 25/09/2026 es la mediana ponderada por monto (RD 142/2026): se ordenan las compras por precio y el TCO es el primer precio en el que el monto acumulado alcanza la mitad del total. El script calcula ambos estadísticos para todos los días y verifica el publicado contra el que corresponde a cada fecha.

Desde el 26/09/2026 el CSV del BCB trae los números como texto-fórmula de Excel (`="4.782"`) y la vigencia puede venir como rango (`2026-09-26 al 2026-09-28`). El parser acepta ambos formatos.

## Definiciones del dashboard

**Dónde cae el TCO**: curva del volumen acumulado por nivel de precio, con el nivel que contiene la mediana. Los colchones son los dólares adicionales que habría que comprar a cualquier precio por debajo (o por encima) del TCO para mover la mediana al nivel vecino: T − 2·(volumen bajo la mediana) y 2·(volumen hasta la mediana) − T, donde T es el volumen total.

**TCO sin este banco**: el TCO recalculado con la fórmula vigente sin las operaciones de ese banco. Efecto = TCO − TCO sin el banco. Con la mediana solo importa de qué lado del TCO cae cada dólar; la tabla muestra ese reparto por banco.

Semana y mes agregan las celdas banco × precio de todos los días del período y aplican la fórmula vigente al último día del período.
