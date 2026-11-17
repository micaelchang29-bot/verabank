# Contribuir

## Commits

- Formato convencional: `tipo(alcance): descripción en minúsculas, en imperativo`.
  Tipos: `feat`, `fix`, `docs`, `test`, `refactor`, `chore`, `ci`, `build`.
- Sin firmas ni pies de herramientas en los mensajes. El hook `.githooks/commit-msg` rechaza el commit
  que los lleve (`git config core.hooksPath .githooks` lo activa).
- Un commit por cambio lógico.

## Pull requests

- La descripción no lleva firmas ni pies de herramientas.
