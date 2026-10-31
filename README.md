# laya-router

Plugin de Hermes Agent que atiende pedidos simples de casa (luces, enchufes, aires) sin pasar por el LLM grande.

Antes de cada turno, el plugin filtra el mensaje con un léxico barato; si parece un pedido de casa, le hace tres preguntas a Laya (a través de Bifrost, `POST /v1/decisions`): si es un pedido de casa, qué acción y sobre qué dispositivo. Con esas respuestas decide una de tres rutas: ignorar el turno (skip), ejecutar directo en Home Assistant (execute) o pasarle una pista al LLM (hint).

En la ruta execute, el plugin llama al servicio de Home Assistant, lee el estado resultante para verificar que se cumplió (con espera y reintentos dentro de `verify_budget_s`) y le inyecta al LLM el resultado ya hecho. En plataformas que no son CLI, además reemplaza la salida del LLM por el mensaje exacto del plugin (por ejemplo `Listo, Luz oficina encendida.`), así que el turno no depende de lo que escriba el modelo. En CLI nunca se reemplaza la salida (hay streaming): solo se inyecta contexto.

Si algo falla (Laya caída, Home Assistant sin respuesta, confianza baja, remitente no permitido), el plugin no bloquea nada: el turno sigue con el LLM, con o sin pista. También registra el tool `home_action` y una sección de system prompt que le explica al LLM cómo leer los bloques `[laya-router]`.

## Estado

Nada de esto fue validado todavía contra un Hermes, un Home Assistant ni un Bifrost reales. Los tests unitarios usan transportes falsos. Antes de confiar en el plugin hay que pasar el checklist de [Verificación](#verificación).

## Requisitos

- Hermes Agent con soporte de plugins.
- Laya detrás de Bifrost (modelo `spanish` por defecto). La URL base de Bifrost no tiene default: sale de una variable de entorno (ver [Variables de entorno](#variables-de-entorno)).
- Python con `pyyaml` disponible para Hermes (el plugin lee `config.yaml`).
- Cuatro variables de entorno (solo se leen del entorno; nunca van en `config.yaml`):
  - `HASS_URL`: URL de Home Assistant.
  - `HASS_TOKEN`: token de larga duración de Home Assistant.
  - `LAYA_BIFROST_URL`: URL base de Bifrost (por ejemplo `https://bifrost.example.com`; una barra final se tolera). El nombre de la variable se puede cambiar con `bifrost_url_env`.
  - `LAYA_BIFROST_API_KEY`: API key de Bifrost. El nombre de la variable se puede cambiar con `bifrost_api_key_env`.

Si falta alguna, el plugin se deshabilita (loguea solo los nombres faltantes) y `/laya status` muestra el motivo.

## Instalación

```
hermes plugins install axelBrei/hermes-laya-router
hermes plugins enable laya-router
```

O a mano: copiar este repo a `~/.hermes/plugins/laya-router/` y habilitarlo con `hermes plugins enable laya-router`. Reiniciar Hermes después.

Antes de habilitarlo, exportar en el entorno del gateway de Hermes las cuatro variables de [Variables de entorno](#variables-de-entorno); sin ellas el plugin queda deshabilitado.

## Variables de entorno

| Variable | Obligatoria | Qué es | Ejemplo |
| --- | --- | --- | --- |
| `HASS_URL` | Sí | URL de Home Assistant. | `http://homeassistant.local:8123` |
| `HASS_TOKEN` | Sí | Token de acceso de larga duración de Home Assistant. | `<token de acceso de larga duración>` |
| `LAYA_BIFROST_URL` | Sí | URL base de Bifrost (una barra final se tolera). | `https://bifrost.example.com` |
| `LAYA_BIFROST_API_KEY` | Sí | API key de Bifrost. | `<api key de Bifrost>` |

Para copiar en el entorno del gateway de Hermes (reemplazar los placeholders por los valores reales, que no van en ningún archivo del repo):

```
export HASS_URL=http://homeassistant.local:8123
export HASS_TOKEN='<token de acceso de larga duración>'
export LAYA_BIFROST_URL=https://bifrost.example.com
export LAYA_BIFROST_API_KEY='<api key de Bifrost>'
```

- Los nombres de las variables de Bifrost se pueden cambiar con `bifrost_url_env` y `bifrost_api_key_env` en `<HERMES_HOME>/laya-router/config.yaml`. La config guarda el nombre de la variable, nunca el valor. `HASS_URL` y `HASS_TOKEN` tienen nombre fijo.
- Las variables se leen al cargar el plugin: hay que reiniciar el gateway de Hermes después de agregarlas o cambiarlas.
- Si falta alguna (o está vacía), el plugin se deshabilita y loguea solo los nombres faltantes, nunca los valores. `/laya status` muestra `laya-router: deshabilitado (faltan variables de entorno: <nombres separados por coma>)` y `/laya refresh` responde `laya-router deshabilitado: faltan variables de entorno: <nombres>.`

Seguridad: nunca poner tokens ni URLs privadas en `config.yaml` ni en el repo.

## Configuración

Archivo opcional: `$HERMES_HOME/laya-router/config.yaml` (por defecto `~/.hermes/laya-router/config.yaml`). Sin archivo se usan los defaults. El cache de entidades se guarda en `entities.json` en el mismo directorio. Claves desconocidas se ignoran con un warning (por ejemplo, la antigua `bifrost_url`: la URL de Bifrost ya no se lee de `config.yaml`, sale de la variable de entorno cuyo nombre indica `bifrost_url_env`); un valor inválido vuelve a su default con un warning.

Defaults completos:

```yaml
# Nombre de la variable de entorno que tiene la URL base de Bifrost (la URL nunca va en este archivo).
bifrost_url_env: "LAYA_BIFROST_URL"
# Modelo de Laya a consultar en /v1/decisions.
bifrost_model: "spanish"
# Nombre de la variable de entorno que tiene la API key de Bifrost.
bifrost_api_key_env: "LAYA_BIFROST_API_KEY"
# Timeout (segundos) de la consulta a Laya; si vence, el turno sigue sin pista.
laya_timeout_s: 6.0
# Timeout (segundos) de cada llamada a la API de Home Assistant.
ha_timeout_s: 5.0
thresholds:
  # Mínimo de is_home para considerar el mensaje un pedido de casa; debajo, skip.
  is_home: 0.5
  # Mínimo de confianza (acción y dispositivo) para ejecutar directo; debajo, hint.
  execute: 0.85
# Dominios de Home Assistant que el plugin conoce y puede controlar.
domains: [light, switch, climate]
# entity_ids que all_off, lights_off y climate_off no apagan. Tras cada refresh se loguea un
# warning por cada id que no exista en Home Assistant (un typo no debe pasar en silencio).
exclude_from_all_off: []
# Remitentes que pueden ejecutar directo (ver "allowed_senders"); vacía = todos.
allowed_senders: []
# Alias por entity_id para ayudar a Laya, por ejemplo: {switch.luz_oficina: [escritorio]}.
aliases: {}
# Cada cuántos minutos se refresca la lista de entidades desde Home Assistant.
cache_refresh_minutes: 15
# Cantidad de turnos previos (usuario + asistente) que se le pasan a Laya como contexto.
history_turns: 2
# Prefijos de palabras; el mensaje debe contener alguna para llegar a Laya.
prefilter_lexicon: [prend, apag, enciend, luz, luces, aire, clima, temperatura, frío, frio, calor, grados, todo]
# Mensajes más largos que esto (caracteres) no se consultan a Laya.
max_message_chars: 300
# Si true, no ejecuta: loguea "[dry-run]" y usa la ruta hint.
dry_run: false
# Tiempo máximo (segundos) esperando que el estado confirme la acción.
verify_budget_s: 3.0
# Intervalo (milisegundos) entre lecturas de estado durante la verificación.
verify_poll_ms: 400
# Máximo de dispositivos candidatos que se le ofrecen a Laya como target. Si la lista se
# corta por este máximo, las acciones sobre un dispositivo van por hint (ver "Cómo decide").
max_target_choices: 12
```

Límites de validación: `laya_timeout_s` y `ha_timeout_s` deben ser > 0; `verify_budget_s` >= 0; `thresholds.*` entre 0 y 1; `cache_refresh_minutes`, `max_message_chars`, `verify_poll_ms` y `max_target_choices` >= 1; `history_turns` >= 0; `domains` y `prefilter_lexicon` no pueden quedar vacías.

### allowed_senders

Controla quién puede disparar la ejecución directa (ruta execute). Recomendado: la forma `platform:id`, por ejemplo `telegram:123456`. Un id suelto (`123456`) coincide con ese id en cualquier plataforma. Lista vacía = cualquiera puede ejecutar directo. El remitente de CLI siempre está permitido.

```yaml
allowed_senders: ["telegram:123456"]
```

Para descubrir tu id, mandá un mensaje desde esa plataforma y corré `/laya status`: la línea `Último remitente visto` muestra `platform:sender_id`.

Fail-closed: si `allowed_senders` es inválida (no es lista, tiene ítems que no son texto ni número, o es `null`), o si `config.yaml` no se puede leer o no es YAML válido, el plugin pasa a solo pista para todo remitente que no sea CLI y loguea un warning.

Advertencia: el tool `home_action` NO consulta `allowed_senders` (el tool no recibe contexto de remitente). La lista solo protege el camino rápido. Un remitente no listado recibe igual la pista "Intención probable", y el LLM puede llamar a `home_action` si la sigue. En un gateway compartido, `allowed_senders` no impide que otro usuario consiga una acción vía LLM; si eso importa, limitá el acceso al gateway o a los tools por otro lado.

### dry_run

Recomendado los primeros días: con `dry_run: true` el plugin decide igual pero no ejecuta. Loguea `[dry-run] would execute ...` y usa la ruta hint. El tool `home_action` tampoco ejecuta en dry_run (responde `status: dry_run`). Un valor inválido de `dry_run` en la config se trata como activado.

## Comandos

- `/laya status`: estado (habilitado o motivo de deshabilitado), umbrales, cantidad de entidades, último refresh, `dry_run`, allowlist, último `platform:sender_id` visto y las últimas 5 decisiones.
- `/laya refresh`: recarga las entidades desde Home Assistant y responde `Entidades: N`.

## Cómo decide

```
mensaje
  |
  +-- prefiltro: vacío, empieza con "/", más de max_message_chars, o sin palabras del léxico
  |       -> sigue el LLM sin tocar nada
  |
  +-- candidatos: dispositivos cuyo nombre/área/alias coincide con el mensaje actual;
  |   el mensaje anterior del usuario (para "apagala") solo se usa si el actual no nombra
  |   ningún dispositivo, nunca desplaza a uno nombrado ahora; si nada coincide se cae a
  |   los del dominio sugerido (luz -> light/switch, aire -> climate) o a todos, en orden
  |   alfabético y cortados a max_target_choices. Si hubo ese fallback o corte, la
  |   lista "no es exacta".
  |
  +-- consulta a Laya (is_home, action, target); si falla o vence -> sigue el LLM sin pista
  |
  +-- is_home < thresholds.is_home, o action = none / desconocida
  |       -> SKIP: sigue el LLM sin pista
  |
  +-- remitente no permitido, o confianza de action < execute,
  |   o (acción sobre un dispositivo y lista de candidatos no exacta),
  |   o (acción sobre un dispositivo y target ausente o con confianza < execute),
  |   o set_climate sin target climate
  |       -> HINT: se inyecta "[laya-router] Intención probable: ..." para que el LLM
  |          llame a home_action o pregunte
  |
  +-- si no: EXECUTE
          dry_run activo -> HINT
          si no: llamada a HA + verificación del estado
            confirmed / sent -> contexto "Acción ya ejecutada" (+ reemplazo de salida fuera de CLI)
            failed           -> contexto "Intenté ... pero ..." (+ reemplazo de salida fuera de CLI)
            error (HA no respondió) -> HINT con aviso, sin reemplazo de salida
```

Ejemplo: con varias luces y "apagá la luz", si ninguna se llama "luz ..." la lista sale por dominio (no exacta) y el plugin solo deja la pista, aunque Laya elija una con confianza alta; el LLM pregunta o llama a `home_action`. Los agregados (`all_off`, `lights_off`, `climate_off`) no dependen de la lista de candidatos. Si varias entidades empatan por nombre (por ejemplo dos "Luz ..." y el mensaje solo dice "luz") y entran todas, la lista es exacta y decide el umbral de confianza del target.

Verificación de la acción contra el texto: además de los umbrales, el plugin no ejecuta por el camino rápido una acción que el texto no respalda. Apagar (`turn_off`, `all_off`, `lights_off`, `climate_off`) exige una palabra de apagado (apag-, desactiv-, off); `turn_on` exige una de encendido (prend-, encend-, activ-, poné) y ninguna de apagado. Si no coincide, queda como pista. Esto cubre el caso en que el clasificador confunde "prende la luz" con `lights_off`.

Detalles de la ejecución:

- Acciones: `turn_on`, `turn_off`, `set_climate` (modo y temperatura 16-30, extraídos del texto), y los agregados `all_off`, `lights_off`, `climate_off`.
- `exclude_from_all_off` aplica a `all_off`, `lights_off` y `climate_off`.
- En los agregados, "Listo, apagué N dispositivos" cuenta solo los confirmados por estado. Las entidades con `assumed_state` (por ejemplo aires por IR) van en otra frase: "A Aire living le mandé la orden sin poder confirmar". Los `unavailable` o `unknown` se informan como "No responden" y no cuentan como falla. Estado del resultado: `confirmed` solo si se confirmaron todos; `sent` si alguno quedó sin confirmar y ninguno falló; `failed` si alguno falló o si no se confirmó ni envió ninguno.
- Entidades con `assumed_state` no se pueden verificar: el resultado es `sent` ("Le mandé ..., pero no puedo confirmar que haya respondido").
- Plazo de ejecución: cada ejecución (llamadas a HA + verificación) tiene un tope de 8 segundos de reloj (constante del código, no es configurable). Vencido el plazo no se hacen más llamadas: una acción simple termina en falla "no pude confirmar a tiempo"; en un agregado, lo que no se llegó a apagar queda como falla ("no llegué a tiempo a apagar") y lo apagado sin confirmar se informa como "No pude confirmar a tiempo". Como cada llamada puede tardar hasta `ha_timeout_s`, el peor caso es unos 8 s + `ha_timeout_s`.
- Si en un agregado Home Assistant no responde (error de red, no un error HTTP de una entidad), se deja de llamar en ese momento y el resto queda como falla ("Home Assistant no respondió"); si no se llegó a apagar nada, el resultado es `error` (pista con aviso). Un timeout en el service call de una acción simple también se informa como `error`, aunque HA pueda haberla ejecutado igual.
- Un mensaje mixto pierde la segunda parte en el camino rápido: si "apagá la luz y decime el clima" se ejecuta directo, fuera de CLI la respuesta entera se reemplaza por el mensaje del plugin ("Listo, ..."), así que la parte del clima no se contesta. Si eso molesta, subí `thresholds.execute` o mandá los pedidos por separado.
- `home_action` con un `entity_id` desconocido refresca las entidades desde Home Assistant como mucho una vez por minuto; dentro de esa ventana responde que no conoce el dispositivo sin refrescar.
- El reemplazo de salida vale 120 segundos desde el turno, y un turno nuevo en la misma sesión lo descarta.
- Cada decisión se loguea en una línea (`laya-router: msg=... action=... conf(...) path=... status=...`).

## Tests

```
python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements-dev.txt && pytest
```

## Verificación

Checklist para hacer en el home lab (con `dry_run: true` hasta el paso 8; se apaga en el paso 9):

1. `python scripts/probe_bifrost.py` (con `LAYA_BIFROST_URL` y `LAYA_BIFROST_API_KEY` exportadas; el script las lee del entorno y no imprime sus valores). Confirmar que cada elemento de `answers` trae `answer_confidence`. Si trae otro nombre, ajustar `parse_answer` en `laya_router/laya_client.py` y su test. (Hoy, si falta, cae a `probability` / probabilidad de la opción elegida / `confidence`.) Confirmar también que la respuesta del predicate (`is_home`) trae `probability`: `is_home` se decide con ese valor, y si no viene el plugin nunca pasa del skip.
2. Confirmar los nombres reales de campos de Bifrost con el mismo script: `probability` (predicate), `choice` (choice), `answer_confidence`, y `name` por pregunta.
3. Instalar el plugin, habilitarlo, poner `dry_run: true` y reiniciar Hermes. Revisar en el log que no haya warnings de registro (por ejemplo, de la sección de system prompt).
4. Confirmar que el tool `home_action` y la sección de system prompt `laya-router.protocol` cargan en la versión de Hermes instalada (el tool aparece disponible; si la versión no soporta secciones, el log lo avisa y se omite).
5. Mandar un mensaje desde cada plataforma y correr `/laya status`: confirmar que `platform` y `sender_id` llegan como strings simples (por ejemplo `telegram:123456`) y que `allowed_senders` coincide con lo que se ve.
6. Correr `/laya refresh` y confirmar que las entidades salen con `area` poblada. Eso valida el template Jinja de `areas` contra el Home Assistant real; si falla, el cache se arma sin áreas.
7. Mandar "prendé la luz de la oficina" y mirar en el log la línea de decisión con las confianzas y `[dry-run]`.
8. Probar cinco frases: encender, apagar, "apagá las luces", "poné el aire en 24", "qué hora es". Ajustar los umbrales si hace falta.
9. Poner `dry_run: false`, reiniciar, repetir "prendé la luz de la oficina" y verificar que la respuesta es exactamente `Listo, Luz oficina encendida.` y que la luz prendió.
