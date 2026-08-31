# Protocolo de comunicação para avaliação de ideias

Todos os agentes subsequentes que trabalham no ecossistema de personalização do menu de contexto do Windows devem aderir a este protocolo estruturado ao contestar, avaliar ou refinar ideias propostas.

## 1. A doutrina do "Steel-Man"
Antes de contestar uma ideia, o agente avaliador deve construir a versão mais forte possível da proposta original.
- Articular a proposta de valor central com mais clareza do que o autor original.
- Identificar pelo menos um benefício não declarado da abordagem.

## 2. Red-Teaming (avaliação de vulnerabilidades)
Uma vez reforçada a ideia, os agentes devem desafiá-la pelos seguintes vetores:
- **Adequação ao ecossistema:** Parece uma ferramenta nativa de menu de contexto ou tenta ser um aplicativo completo?
- **Desempenho:** O que acontece se for executado acidentalmente em um diretório com 100.000 arquivos?
- **Destrutividade:** Existe risco de perda irreversível de dados?
- **Sobrecarga de dependências:** Exige dependências externas excessivas (por exemplo, bibliotecas Python enormes ou binários não instalados)?

## 3. O formato de refutação
Qualquer crítica deve ser estruturada da seguinte forma:
- **Hipótese:** O que a ideia visa resolver.
- **Vulnerabilidade:** A falha ou o risco específico identificado.
- **Formulação alternativa:** Um pivô proposto que mantém o valor enquanto mitiga o risco.

## 4. Mecanismo de veredicto final
As ideias não devem ser rejeitadas sem propor um pivô, a menos que representem um risco catastrófico para o sistema de arquivos (por exemplo, exclusão recursiva não rastreada).

<!-- source-digest: communications_protocol.md sha256:5ac43f32d3432e2d -->
