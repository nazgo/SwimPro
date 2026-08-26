# SwimPro V39 Cloud

Esta versión está preparada para desplegarse en Render con HTTPS.

## Archivos añadidos
- requirements.txt
- render.yaml
- Procfile
- .python-version
- configuración ProxyFix para HTTPS detrás del proxy de Render

## Importante sobre los datos
La aplicación actual usa almacenamiento local/SQLite según la lógica existente del proyecto.
En un servicio cloud sin disco persistente, un redeploy puede borrar esos datos.

Para una versión personal de prueba puedes desplegar la aplicación primero y validar la PWA.
Antes de usarla como almacenamiento definitivo, conviene migrar la base de datos a PostgreSQL
o contratar/configurar almacenamiento persistente compatible con el proveedor.

## Inicio
Render ejecuta:
gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 120

Una vez desplegada tendrás una URL HTTPS pública. Esa URL podrá abrirse desde cualquier lugar
sin que el computador personal esté encendido y podrá instalarse como PWA.
