# laya-router: diseño

Plugin de Hermes Agent que, antes de cada llamada al LLM, clasifica el mensaje del usuario con Laya (vía Bifrost) para detectar acciones domóticas conocidas y, cuando la confianza es alta, las ejecuta contra Home Assistant sin esperar al loop de tools del LLM.

Fecha: 2026-10-30. Estado: borrador para aprobación.

> Nota (2026-10-31): este documento es un registro histórico. La configuración se cambió después a solo-entorno: la URL de Bifrost ya no es una clave `bifrost_url` con default, sino que sale de la variable de entorno cuyo nombre indica `bifrost_url_env` (por defecto `LAYA_BIFROST_URL`), y la API key de `bifrost_api_key_env` (por defecto `LAYA_BIFROST_API_KEY`). Ver el README.

## Intención y por qué

Hoy un pedido como "prendé la luz de la oficina" pasa por el flujo completo de Hermes: el LLM lee el mensaje, decide invocar un tool de Home Assistant (apoyándose en una skill que describe la API), espera el resultado y recién ahí redacta la respuesta. Son al menos dos llamadas al LLM más la latencia del tool, para una decisión que es trivial y repetitiva.

Laya es un clasificador no generativo (encoder tipo ModernBERT, ~35 ms por pregunta) que responde preguntas tipadas con confianza calibrada. Sirve exactamente para esto: detectar en milisegundos si el mensaje es una acción conocida y cuál. El plugin usa esa decisión para ejecutar la acción por adelantado y dejarle al LLM solo la tarea de confirmar.

El objetivo es que las acciones de casa se sientan instantáneas y consuman una sola llamada al LLM, sin que el modelo tenga que leer skills ni razonar sobre la API de Home Assistant.

## Goals

- Detectar y ejecutar acciones sobre luces, enchufes y aires acondicionados a partir de lenguaje natural en español, antes de que el LLM corra.
- Soportar "apagá todo" como acción agregada.
- Reducir cada acción exitosa a exactamente una llamada al LLM, con una respuesta corta y determinista.
- Funcionar igual en gateway (Telegram, Discord, etc.) y en CLI/TUI, con un solo código.
- Nunca confirmar una acción que no se verificó contra el estado real de Home Assistant.
- Degradar con gracia: si Laya, Bifrost o Home Assistant fallan, el turno sigue como un turno normal de Hermes.

## Non-goals

- No reemplaza la integración existente de Home Assistant en Hermes para pedidos complejos (automatizaciones, escenas, consultas históricas). Esos siguen el flujo normal.
- No usa el fast path de `post_gateway_admission` (saltear el LLM del todo) en esta versión. Queda documentado como extensión futura.
- No cubre cerraduras, alarmas, persianas, ni nada cuya ejecución equivocada tenga costo real. La v1 se limita a dominios idempotentes y reversibles.
- No hace extracción de parámetros con Laya más allá de clasificación. La temperatura se extrae con regex.

## Anti-goals (qué sería un fracaso aunque "funcione")

- Que el plugin diga "listo" y la luz siga apagada.
- Que ejecute una acción sobre la entidad equivocada por una clasificación de confianza media.
- Que un error del plugin bloquee o rompa el turno normal de Hermes.
- Que el LLM reciba la pista de Laya y aun así se ponga a leer la skill de Home Assistant o a llamar tools genéricos. La pista tiene que hacer que la respuesta sea rápida.
- Que el system prompt cambie de turno a turno y rompa el cache de prompt.

## Contexto técnico (verificado en docs, 2026-10-30)

### Hooks de Hermes relevantes

- `pre_llm_call`: corre una vez por turno, después de la compresión de contexto y antes del loop de tools. Recibe `session_id`, `user_message`, `conversation_history`, `is_first_turn`, `model`, `platform`, entre otros. Si devuelve `{"context": "..."}` o un string, el texto se agrega al mensaje del usuario del turno actual. No puede evitar la llamada al LLM. Acotado por `plugins.hook_callback_timeout` (30 s por defecto).
- `transform_llm_output`: corre antes de la entrega final. Recibe `response_text`, `session_id`, `model`, `platform`. El primer string no vacío reemplaza la respuesta.
- `ctx.register_system_prompt_section(id, text_or_callable, position="after_memory", max_chars=4000)`: sección fija en el system prompt, renderizada una vez por sesión, cache-safe.
- `ctx.register_tool(name, toolset, schema, handler)`: registra un tool que el LLM puede llamar.
- Slash commands y CLI commands también se registran desde `register(ctx)`.
- Ningún hook permite saltear el LLM en CLI/TUI/Desktop. Solo `post_gateway_admission` lo permite, y solo en gateway. Por eso v1 no lo usa.

Fuente: https://hermes-agent.nousresearch.com/docs/user-guide/features/hooks y https://hermes-agent.nousresearch.com/docs/user-guide/features/plugins

### Laya vía Bifrost

Laya está desplegado detrás de Bifrost (URL de ejemplo: `https://bifrost.example.com`). El modelo configurado se llama `spanish`. La URL base está en la variable de entorno `LAYA_BIFROST_URL` y la API key en `LAYA_BIFROST_API_KEY`, ambas del gateway. El plugin las lee del entorno y nunca las loguea.

Endpoint: `POST /v1/decisions`, header `x-api-key`. Cuerpo:

```json
{
  "model": "spanish",
  "input": "<texto del usuario, o lista de mensajes role/content>",
  "questions": [
    {"type": "predicate", "name": "is_home", "instructions": "..."},
    {"type": "choice", "name": "action", "instructions": "...",
     "choices": [{"value": "turn_on", "description": "..."}, ...]},
    {"type": "choice", "name": "target", "instructions": "...",
     "choices": [{"value": "light.living", "description": "Luz del living"}, ...]}
  ]
}
```

Respuesta: `answers` es una lista alineada con `questions`. Cada una trae `type`, `name`, y según tipo `probability` (predicate) o `choice` más `probabilities` (choice). Laya además devuelve `answer_confidence` (probabilidad calibrada). Los campos `abstention`, `abstention_threshold` y `low_confidence` aparecen cuando se pide `min_confidence`; la doc de Bifrost no muestra ese campo en el request, así que **el plugin decide umbrales por su cuenta usando `answer_confidence` o, si falta, la probabilidad del `choice` elegido**. El builder debe verificar con una llamada real qué campos devuelve `spanish` y adaptar el parser; el contrato interno del plugin (ver más abajo) no cambia.

Fuente: https://docs.getbifrost.ai/api-reference/decisions/evaluate-a-decision y https://github.com/NandhaKishorM/laya

### Home Assistant

`HASS_URL` y `HASS_TOKEN` se leen del entorno del gateway. REST API:

- `POST {HASS_URL}/api/services/<domain>/<service>` con `{"entity_id": ...}` y parámetros. Devuelve 200 y la lista de estados que cambiaron. Lista vacía si no cambió nada (por ejemplo, la luz ya estaba prendida).
- `GET {HASS_URL}/api/states/<entity_id>` devuelve el estado actual. Es la verificación fuerte.
- `GET {HASS_URL}/api/states` devuelve todas las entidades, para la cache.
- `POST {HASS_URL}/api/template` con un template Jinja para obtener `area_name(entity_id)`, que `/api/states` no incluye.

Dato conocido del entorno del usuario: "Luz oficina" es `switch.luz_oficina`, un enchufe que no está asignado al área Oficina. El nombre amigable es la señal principal; el área es secundaria.

## Alcance funcional v1

### Dominios y acciones

| Dominio HA | Acciones | Parámetros |
|---|---|---|
| `light` | encender, apagar | ninguno (brillo y color quedan fuera) |
| `switch` | encender, apagar | ninguno. Se incluye porque algunas "luces" son enchufes |
| `climate` | encender, apagar, setear modo, setear temperatura | modo: frío, calor, ventilador, auto. Temperatura: entero 16–30 |
| agregado | apagar todo | recorre todas las entidades `light`, `switch` y `climate` de la cache y las apaga |
| agregado por dominio | apagar las luces, apagar los aires | `lights_off` apaga `light` y `switch`; `climate_off` apaga `climate` |

Agregados por área ("apagá lo del living") y multi-target ("prendé la luz y el aire") quedan fuera del camino rápido en v1: Laya los clasifica como `none` o con baja confianza y caen al camino con pista, donde el LLM pregunta o resuelve con varias llamadas a `home_action`. Decisión del usuario.

"Apagar todo" y los agregados por dominio no piden confirmación: las acciones son reversibles y de bajo costo. Si querés excluir un switch de "apagar todo" (una heladera enchufada, por ejemplo), hay una lista de exclusión en la config del plugin.

Decisión del usuario: `switch` entra porque `luz_oficina` es un switch. Decisión mía: "apagar todo" excluye entidades listadas en `exclude_from_all_off`.

### Flujo por turno

1. `pre_llm_call` recibe `user_message`. Si es multimodal, se toma solo la parte de texto. Si está vacío o es un slash command, el plugin no hace nada.
2. Filtro barato local: si el mensaje supera cierta longitud (decisión mía: 300 caracteres) o no contiene ninguna palabra de un pequeño léxico de domótica (prender, apagar, luz, aire, clima, temperatura, frío, calor, todo, y variantes), el plugin devuelve `None` sin llamar a Laya. Esto evita gastar 35 ms y una llamada de red en cada mensaje de conversación general. El léxico es configurable.
3. Llamada a Laya con las tres preguntas (`is_home`, `action`, `target`). El `target` se arma desde la cache de entidades. Timeout de la llamada: 2 s (decisión mía). Si falla o expira, se registra en log y el plugin devuelve `None`.
4. Allowlist de remitentes (decisión del usuario: el gateway lo usan varias personas): `pre_llm_call` recibe `sender_id` y `platform`. Si `allowed_senders` en la config no está vacío y el `sender_id` del turno no está en la lista, el camino rápido queda deshabilitado para ese turno y la decisión salta directo al camino con pista (paso 8). En CLI `sender_id` viene vacío y se trata como el dueño. El builder confirma el formato exacto de `sender_id` por plataforma (Telegram y Discord) y lo documenta en el README.
5. Decisión por umbrales (decisión mía, configurables):
   - `is_home < 0.5`: no es domótica. Devuelve `None`.
   - `is_home >= 0.5` y `action` y `target` con confianza `>= 0.85`: **camino rápido**. Ir a 6.
   - Cualquier otro caso con `is_home >= 0.5`: **camino con pista**. Ir a 8.
6. Camino rápido: ejecutar la acción contra Home Assistant (POST al service), luego verificar con GET del estado. Para `climate` con temperatura, verificar `attributes.temperature`. Para "apagar todo", verificar cada entidad; reportar las que quedaron encendidas.

   Verificación con espera (decisión mía): muchos dispositivos (Zigbee, Tuya cloud) tardan en reflejar el cambio. El GET se reintenta cada 400 ms hasta un presupuesto total de 3 s. Si al final el estado coincide, es "confirmado". Si la entidad tiene `attributes.assumed_state: true` (típico de aires por infrarrojo, donde HA reporta lo que mandó y no lo que pasó), no hay verificación posible: el resultado es "enviado, sin confirmación" y el texto para el usuario lo dice así ("Le mandé apagar al aire del living"), nunca "listo". Si el presupuesto se agota sin coincidencia, es fallo verificado.

   Modo `dry_run` (decisión mía): con `dry_run: true` en la config, el plugin clasifica, loguea la decisión completa y lo que hubiera ejecutado, pero no llama a ningún service de HA ni inyecta "ejecutado"; el turno sigue como camino con pista. Es el modo para calibrar umbrales con frases reales contra la casa real sin riesgo, y el modo recomendado para los primeros días (shadow mode). No hay Home Assistant de pruebas; los tests automáticos usan HA mockeado.
7. Guardar el resultado en un registro por `session_id` (en memoria, con TTL de un turno) e inyectar contexto:
   - Éxito: `[laya-router] Acción ya ejecutada y verificada: <descripción humana>. Respondé con una sola frase confirmando. No llames a ningún tool.`
   - Fallo verificado (HA respondió pero el estado no cambió, o un dispositivo no respondió): `[laya-router] Intenté <acción> pero <entidad> sigue en <estado>. Informale al usuario. No digas que se hizo.`
   - Error de red o auth con HA: no se inyecta "ejecutado". Se cae al camino con pista con una nota de que HA no respondió.
8. Camino con pista: inyectar `[laya-router] Intención probable: <acción> sobre <entidad> (confianza X). Si estás de acuerdo, llamá al tool home_action con esos parámetros. Si hay ambigüedad, preguntá.` El LLM resuelve con el tool del plugin.
9. `transform_llm_output`: si hay un registro de ejecución para este `session_id` en este turno, reemplaza la respuesta del LLM por el texto de confirmación o de fallo generado por el plugin. Después borra el registro. Esto garantiza la salida aunque el LLM ignore la instrucción. Si no hay registro, devuelve `None`.

   Excepción para CLI con streaming (decisión mía, basada en la doc de Hermes): en la CLI clásica el texto streameado ya se mostró y el reemplazo se imprime después como "post-stream transformation", así que el usuario vería dos respuestas. Cuando `platform == "cli"` el plugin no reemplaza y confía en la instrucción inyectada. En TUI, Desktop y gateway la respuesta streameada se reemplaza en el lugar, así que ahí sí se aplica.

Nota sobre `conversation_history`: el contexto inyectado queda en el historial como parte del mensaje del usuario. Es deseable, porque turnos siguientes ("y ahora apagala") ven qué se hizo. El builder debe confirmar que el prefijo `[laya-router]` no confunde al LLM en turnos posteriores; si lo hace, la sección de system prompt lo explica.

### Tool `home_action`

Registrado con `ctx.register_tool`. Schema:

```json
{
  "name": "home_action",
  "description": "Ejecuta una acción sobre luces, enchufes o aires acondicionados de la casa y verifica el resultado. Usalo cuando el mensaje traiga una intención [laya-router] o el usuario pida una acción simple de casa.",
  "parameters": {
    "type": "object",
    "properties": {
      "action": {"type": "string", "enum": ["turn_on", "turn_off", "all_off", "lights_off", "climate_off", "set_climate"]},
      "entity_id": {"type": "string", "description": "entity_id de Home Assistant. Omitir para all_off, lights_off y climate_off."},
      "hvac_mode": {"type": "string", "enum": ["cool", "heat", "fan_only", "auto"]},
      "temperature": {"type": "integer", "minimum": 16, "maximum": 30}
    },
    "required": ["action"]
  }
}
```

El handler usa exactamente la misma función de ejecución y verificación que el camino rápido. Devuelve JSON con `ok`, `entity_id`, `state_before`, `state_after`, `message`.

### Sección de system prompt

Una sola sección, id `laya-router.protocol`, posición `after_memory`, bien por debajo de 4000 caracteres. Contenido aproximado (el builder ajusta la redacción):

> Un plugin clasifica los pedidos de casa antes de que vos los veas. Si el mensaje del usuario termina con un bloque `[laya-router]`:
> - "Acción ya ejecutada": respondé con una sola frase corta confirmando lo que dice el bloque. No llames a ningún tool, no leas skills, no expliques.
> - "Intenté ... pero ...": informá el problema en una frase. No digas que se hizo.
> - "Intención probable": si es inequívoco, llamá a `home_action` con esos parámetros. Si hay dudas, hacé una pregunta corta.
> Para acciones simples de luces, enchufes y aire acondicionado usá siempre `home_action`, no la API de Home Assistant directamente.

### Cache de entidades

- Archivo: `<HERMES_HOME>/laya-router/entities.json`. El plugin usa `ctx.session_cwd()` o la variable `HERMES_HOME` para resolver la ruta, nunca `os.getcwd()` (restricción documentada del gateway).
- Contenido por entidad: `entity_id`, `domain`, `friendly_name`, `area` (puede ser null), `state`, `aliases` (lista opcional que el usuario puede agregar a mano en la config, por ejemplo `["luz oficina", "la de la oficina"]`).
- Carga: en `register(ctx)` se lee el archivo si existe. Si no existe, se hace un refresh inmediato en un thread para no bloquear el arranque.
- Refresh: un thread de fondo cada 15 minutos (configurable). Hace `GET /api/states`, filtra dominios `light`, `switch`, `climate`, y `POST /api/template` para áreas. Escritura atómica (archivo temporal más rename).
- Refresh forzado: cuando Laya devuelve un `target` que no está en cache, o cuando la verificación da 404. Se refresca y se reintenta una vez.
- Slash command `/laya refresh` fuerza el refresh y reporta cuántas entidades hay. `/laya status` muestra umbrales, cantidad de entidades, hora del último refresh y últimas decisiones.

Las descripciones de los `choices` de `target` se arman como `"<friendly_name>[, área <area>][, alias: ...]"`. El `choices` incluye siempre una opción `none` con descripción "ninguna entidad concreta o no aplica".

Candidatos acotados (decisión mía; el usuario tiene entre 10 y 25 entidades en estos dominios): para que la confianza no se diluya, `target` no manda toda la cache. Se arma una lista de candidatos con las entidades cuyo `friendly_name`, `area` o `aliases` comparten algún token (normalizado, sin acentos, con stem simple) con el mensaje del usuario, hasta un máximo de 12. Si ningún candidato matchea, se mandan todas las entidades del dominio que sugiere el léxico (luz/luces → `light` y `switch`, aire/clima → `climate`), con el mismo tope. Si aun así supera el tope, se mandan las primeras 12 por orden alfabético y la decisión cae a camino con pista salvo confianza muy alta. El builder ajusta el tope con la cantidad real de entidades.

### Preguntas a Laya

Tres preguntas, en español, en una sola llamada:

- `is_home` (predicate): "¿El usuario pide ejecutar una acción sobre un dispositivo de la casa (luz, enchufe, aire acondicionado), ahora, en este mensaje?" Los criterios aclaran que preguntas, comentarios y pedidos de otra cosa son `false`.
- `action` (choice): `turn_on`, `turn_off`, `all_off`, `lights_off`, `climate_off`, `set_climate`, `none`. `set_climate` cubre modo y temperatura de aires; encender o apagar un aire concreto es `turn_on`/`turn_off`. Las descripciones tienen que distinguir claramente "apagá todo" de "apagá las luces" para que no se apague el aire por error.
- `target` (choice): una opción por entidad candidata más `none`. Para `all_off`, `lights_off` y `climate_off` se espera `none`.

El `input` se envía como lista de mensajes: el mensaje actual del usuario más, como máximo, el turno anterior (usuario y asistente) tomado de `conversation_history`, para resolver referencias como "apagala". Decisión mía: dos turnos de historial, configurable.

Parámetros de clima se extraen localmente del texto del usuario:
- temperatura: regex sobre enteros entre 16 y 30, opcionalmente seguidos de "grados" o "°".
- modo: léxico simple (frío/fresco → `cool`, calor/calefacción → `heat`, ventilador → `fan_only`, automático → `auto`). Si no hay modo ni temperatura y la acción es `set_climate`, se trata como `turn_on`.

### Configuración del plugin

Archivo `plugin.yaml` con `requires_env: [HASS_URL, HASS_TOKEN, LAYA_BIFROST_URL, LAYA_BIFROST_API_KEY]`. Sin capabilities privilegiadas: el plugin no override tools ni modelos.

Entorno incompleto (decisión mía): si al cargar falta alguna de las cuatro variables, o el primer refresh de cache falla por red, el plugin se marca como deshabilitado para esa sesión, loguea una sola línea de warning y todos sus hooks devuelven `None`. No reintenta en cada turno ni muestra nada al usuario. Esto cubre una CLI corriendo fuera del home lab sin acceso a Home Assistant. El refresh de cache en segundo plano sigue intentando con backoff y rehabilita el plugin si vuelve a tener conectividad.

Config de usuario en `<HERMES_HOME>/laya-router/config.yaml` con defaults razonables:

```yaml
bifrost_url_env: LAYA_BIFROST_URL
bifrost_model: spanish
bifrost_api_key_env: LAYA_BIFROST_API_KEY
laya_timeout_s: 2.0
ha_timeout_s: 5.0
thresholds:
  is_home: 0.5
  execute: 0.85
domains: [light, switch, climate]
exclude_from_all_off: []
allowed_senders: []   # vacío = todos; si no, lista de sender_id por plataforma, ej. "telegram:123456"
aliases: {}
cache_refresh_minutes: 15
history_turns: 2
dry_run: false
verify_budget_s: 3.0
verify_poll_ms: 400
max_target_choices: 12
prefilter_lexicon: [prend, apag, enciend, luz, luces, aire, clima, temperatura, frío, frio, calor, grados, todo]
max_message_chars: 300
```

## Decisiones ya tomadas

Por el usuario:
- Laya se usa a través de Bifrost, modelo `spanish`, con la API key del entorno indicada.
- `HASS_URL` y `HASS_TOKEN` del gateway son la vía a Home Assistant.
- Dominios v1: luces y aires acondicionados. "Apagá todo" tiene que funcionar.
- El camino con pista usa un tool propio del plugin (`home_action`), no la skill ni la integración existente de Home Assistant. El LLM tiene que procesar rápido.
- La integración existente de Home Assistant en Hermes sigue habilitada en paralelo. El plugin no la deshabilita ni declara `tools.override`; compite solo con la sección de system prompt y la descripción del tool. Si en la práctica el LLM se desvía a los tools de HA, se revisa la redacción antes de escalar a override.
- Agregados en v1: "apagá todo", "apagá las luces", "apagá los aires". Por área y multi-target quedan para el camino con pista.
- El gateway lo usan varias personas: el camino rápido se limita a `allowed_senders`.
- Entre 10 y 25 entidades en los dominios cubiertos.
- La ejecución anticipada en `pre_llm_call` con contexto de "ya ejecutado" es la estrategia central.
- Repo: `~/Documents/hermes-laya-router`, a publicar como `axelBrei/hermes-laya-router`.

Mías (el usuario puede revertirlas):
- `switch` entra en v1 para cubrir enchufes que funcionan como luces.
- Umbrales 0.5 / 0.85, timeout Laya 2 s, HA 5 s, cache 15 min, historial 2 turnos.
- Prefiltro léxico antes de llamar a Laya.
- `transform_llm_output` como red de seguridad para forzar la respuesta.
- "Apagar todo" sin confirmación, con lista de exclusión.
- Temperatura y modo por regex y léxico, no por Laya.
- `post_gateway_admission` fuera de v1.
- Lenguaje: Python 3.11+, solo stdlib más `requests` (o `httpx`) y `pyyaml`. Sin frameworks.

## Qué queda para el builder

- Estructura interna de módulos (sugerencia: `__init__.py` con `register`, `laya_client.py`, `ha_client.py`, `entities.py`, `router.py`, `config.py`).
- Verificar con una llamada real a Bifrost el formato exacto de `answers` para el modelo `spanish` y ajustar el parser.
- Redacción final de instrucciones y criterios de las preguntas a Laya, y de la sección de system prompt. Probar con un set de frases reales en español (prender, apagar, ambiguas, no domóticas) y ajustar umbrales.
- Estrategia de tests: unitarios con Bifrost y Home Assistant mockeados por HTTP; un test de integración opcional contra el Bifrost real marcado para correr a mano.
- Logging: una línea por decisión (mensaje truncado, acción, target, confianzas, camino tomado, latencias), sin API keys ni tokens.
- README con instalación en el home lab (`hermes plugins install axelBrei/hermes-laya-router`, habilitar en `config.yaml`, variables de entorno requeridas).
- Manejo de `user_message` multimodal y de mensajes vacíos.
- Cómo se comporta si `register_system_prompt_section` o `register_tool` no existen en la versión de Hermes del home lab (degradar con warning, no romper).

## Extensiones futuras (fuera de v1)

- Fast path en `post_gateway_admission` para saltear el LLM del todo en Telegram y similares.
- Más dominios: `cover`, `scene`, `media_player`.
- Agregados por área ("apagá lo del living") y multi-target en el camino rápido.
- Brillo y color en luces.
- Usar `pre_tool_call` para que Laya valide que un tool de Home Assistant invocado por el LLM coincide con la intención del usuario.
