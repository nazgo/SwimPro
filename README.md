# SwimPro V31 · Achievements

Nuevo módulo Logros personales.

Los logros se calculan automáticamente desde los datos existentes:
- Primera marca
- Nuevo PB
- 5 / 10 / 25 nuevos PB
- Primera competencia
- 5 / 10 / 25 competencias
- 10 / 25 / 50 / 100 tiempos
- 5 / 10 / 15 pruebas distintas
- Objetivos cumplidos
- Sub 1:30, Sub 1:20, Sub 1:15 y Sub 1:10 en 100 m
- 50 m Sub 30
- Logros específicos de 100 Mariposa
- Rachas de mejoras

Incluye:
- porcentaje total de logros desbloqueados
- logros conseguidos
- próximos logros
- barras de progreso en logros cuantitativos
- acceso directo desde Inicio

No requiere cambios manuales en `swimtracker.db`.


## V38 · PWA
- Manifest instalable
- Android/iPhone standalone
- Iconos 192/512 + maskable
- Apple Touch Icon
- Splash screens
- Service Worker
- Cache estática + fallback offline
- Safe areas para notch
- Instalación desde Perfil
- Shortcuts de app
- Requiere HTTPS en producción (localhost funciona para pruebas)


## V41 · Usuarios & Login

- Registro de usuarios
- Login / logout
- Contraseñas almacenadas mediante hash seguro de Werkzeug
- Sesiones Flask seguras
- Cada registro PostgreSQL incluye `user_id`
- Row Level Security (RLS) en Neon para:
  - profile
  - swims
  - competitions
  - competition_events
  - goals
  - splits
- Un usuario no puede leer, editar ni eliminar datos de otro usuario aunque conozca el ID de la URL.
- La primera cuenta registrada recibe automáticamente todos los datos existentes de V40.
- Las cuentas posteriores comienzan con un perfil y datos completamente independientes.

### Importante al desplegar V41
Después del deploy crea TU cuenta antes de compartir públicamente la URL.
La primera cuenta es la que reclamará automáticamente los datos históricos existentes.

La funcionalidad multiusuario segura requiere PostgreSQL/Neon.
SQLite local se mantiene únicamente como modo de desarrollo de un solo usuario.
