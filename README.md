# TCO Bolivia — bases de cálculo del Tipo de Cambio Oficial

Descarga cada día las operaciones de compra de dólares que el Banco Central de Bolivia publica como base de cálculo del TCO, las guarda en este repositorio y las muestra en un dashboard estático.

## Cómo funciona

`scripts/fetch_tco.py` consulta la página del BCB (`bcb_tco_publico_detalle_historico.php`), lee la lista de fechas de corte publicadas, descarga el CSV de cada fecha que aún no está en `data/raw/` y reconstruye las tablas derivadas:

| Archivo | Contenido |
|---|---|
| `data/raw/AAAA-MM-DD.csv` | CSV original del BCB, sin tocar |
| `data/operaciones.csv` | una fila por fecha de corte × banco × tipo de cambio: número de operaciones y monto en USD |
| `data/diario.csv` | una fila por fecha de corte × banco: monto, operaciones y TC ponderado |
| `data/verificacion.csv` | TCO recalculado a partir de las celdas vs. TCO publicado, por día |
| `data/tco.json` | todo lo anterior, para el dashboard |
| `data/tco.db` | SQLite con las tablas `operaciones`, `diario` y `total` |

El workflow `.github/workflows/actualizar.yml` corre el script a la 01:30 UTC (21:30 de La Paz, tras la publicación de las 20:00) y de nuevo a las 13:00 UTC, y hace commit de los datos nuevos. También se puede lanzar a mano desde la pestaña Actions.

`index.html` es el dashboard: lee `data/tco.json` y se publica con GitHub Pages desde la raíz de la rama `main`.

## Correr localmente

```
python3 scripts/fetch_tco.py            # descarga lo que falte y reconstruye
python3 scripts/fetch_tco.py --sin-descarga   # solo reconstruye desde raw/
python3 -m http.server 8000             # abrir http://localhost:8000
```

Solo requiere Python 3.10 o superior, sin dependencias externas.

## Definiciones del dashboard

El TCO es el promedio de los tipos de cambio ponderado por el monto en dólares. Para un banco *b* con participación *s* en los dólares del día y TC ponderado *r*, el **empuje** es (r − TCO) × s: cuánto alejó ese banco el TCO del promedio, en bolivianos (la tabla lo muestra en centavos). La suma de los empujes es cero por construcción. El **aporte al cambio** es s·r menos el mismo producto en el período anterior; la suma sobre los bancos es exactamente la variación del TCO. Semana y mes agregan las celdas de todos los días del período y recalculan el promedio ponderado.
