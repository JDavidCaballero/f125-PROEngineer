# Cómo colaborar

1. Haz un fork (o clona) y crea una rama por tema: `git switch -c voz/mejora-jerga`.
2. Prepara el entorno: solo hace falta Python 3.11+ para el motor y las pruebas. Para voz en Mac: `./setup_mac.sh` (lee el script antes; no usa `sudo`).
3. Tu configuración personal (IP del servidor del modelo, nombre del modelo…) va en `config.local.toml`, que no se sube: ver `docs/CONFIGURACION.md`.
4. Pruebas: `python3 -m unittest discover -s tests < /dev/null`. Deben pasar antes de abrir un PR.
5. Un cambio de comportamiento lleva su prueba. Si el cambio sale de un fallo visto en una sesión real, cita el informe/dato en el mensaje del commit.
6. Lo que el ingeniero dice al piloto no puede inventar datos (mira `docs/ARQUITECTURA.md`).
7. Nunca subas capturas del juego, audios, informes con tu usuario/rutas ni tu perfil aprendido (`.gitignore` ya los deja fuera). No subas claves ni tokens.

Estado, límites conocidos e ideas por área: `docs/PENDIENTES.md`.

**Licencia de los aportes:** al enviar un Pull Request aceptas que tu aporte se publique bajo la misma licencia del proyecto (GPL-3.0). Añade `Signed-off-by: Tu Nombre <correo>` a tus commits (`git commit -s`) para confirmar que tienes derecho a aportarlo.
