# Protocolo de comunicación para la evaluación de ideas

Todos los agentes posteriores que trabajen en el ecosistema de personalización del menú contextual de Windows deben adherirse a este protocolo estructurado al cuestionar, evaluar o refinar ideas propuestas.

## 1. La doctrina del "hombre de paja inverso" (Steel-Man)
Antes de cuestionar una idea, el agente evaluador debe construir la versión más fuerte posible de la propuesta original.
- Articular la propuesta de valor central con más claridad que el autor original.
- Identificar al menos un beneficio no declarado del enfoque.

## 2. Red-Teaming (evaluación de vulnerabilidades)
Una vez reforzada la idea, los agentes deben desafiarla a través de los siguientes vectores:
- **Adecuación al ecosistema:** ¿Parece una herramienta nativa de menú contextual o intenta ser una aplicación completa?
- **Rendimiento:** ¿Qué sucede si se ejecuta accidentalmente en un directorio con 100.000 archivos?
- **Destructividad:** ¿Existe riesgo de pérdida de datos irrecuperables?
- **Sobrecarga de dependencias:** ¿Requiere dependencias externas excesivas (por ejemplo, bibliotecas Python masivas o binarios no instalados)?

## 3. El formato de refutación
Toda crítica debe estructurarse de la siguiente manera:
- **Hipótesis:** Lo que la idea pretende resolver.
- **Vulnerabilidad:** El defecto o riesgo específico identificado.
- **Formulación alternativa:** Un giro propuesto que conserva el valor mientras mitiga el riesgo.

## 4. Mecanismo de veredicto final
Las ideas no deben rechazarse de plano sin proponer un giro, a menos que representen un riesgo catastrófico para el sistema de archivos (por ejemplo, eliminación recursiva no rastreada).

<!-- source-digest: communications_protocol.md sha256:5ac43f32d3432e2d -->
