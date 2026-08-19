---
repo: architecture
path: docs/architecture/aw-app-notion.md
source: generated
edited: false
checksum: sha256:97b9eea4a13ed9da53a85051b455ecd567a48ab61eb10d1d4adc5dc953ee5b4d
---
# Notion

- **repo**: aw-app-notion
- **layer**: app
- **technologies**: python
- **health** (derived): planned

Ports agentic-workspace's Notion integration into aw-workspace: stores a Notion internal-integration token in the zero-knowledge secret store and generates the mcp.json entries MCP Gateway scans — both the generic @notionhq/notion-mcp-server and this app's own aw-kanban server, which turns a Notion database into the Agents Kanban board (list/create/move/comment on cards, set QA status, flag blockers).

## Connections
- `http` → **aw-workspace** — routes mounted at /api/apps/notion
- `stdio-mcp` → **mcp-gateway** — MCP surface aggregated by the gateway

## MCP tools
- `add_kanban_comment`
- `attach_kanban_file`
- `attach_kanban_presentation`
- `comments`
- `create-a-data-source`
- `create_kanban_task`
- `get_kanban_card`
- `get_kanban_properties`
- `list-data-source-templates`
- `list_kanban_cards`
- `move_kanban_task`
- `move-page`
- `query-data-source`
- `retrieve-a-database`
- `retrieve-a-data-source`
- `retrieve-page-markdown`
- `search`
- `set_blocker`
- `set_kanban_property`
- `set_qa_status`
- `update-a-data-source`
- `update-page-markdown`

## Requirements
### As notas sincronizadas caem na árvore do KB, não no diretório do app
- Given no monolito o destino era docs/knowledge_base/notes/ dentro do próprio repo, mas aqui o KB é um app separado, com árvore própria e durável
- When o destino é resolvido (repos/aw-app-notion/notion_app/sync.py::kb_dir:81 e notes_dir:85, ancorados em workspace_home:69)
- Then as notas são escritas sob &lt;AW_WORKSPACE_HOME&gt;/knowledge_base, que é o que o aw-app-kb monta e indexa, e nunca sob o diretório do pacote do app — escrever no diretório do app produziria uma sincronização que roda, relata sucesso, cria arquivos de verdade, e não aparece em busca nenhuma, porque o indexador olha para outro lugar. É a diferença entre sincronizar e sincronizar para o vazio
- intended_status: `not_implemented` · derived health: `not_implemented`
- tests: `repos/aw-app-notion/tests/test_sync.py` (passing)

### Página que não mudou é pulada, comparando hash e não data
- Given um estado salvo da execução anterior, com o conteúdo já visto de cada página
- When uma segunda execução compara o hash do conteúdo renderizado (repos/aw-app-notion/notion_app/sync.py::_sha256:99, contra o estado carregado por _load_state:242)
- Then páginas inalteradas são puladas, uma página editada conta como atualizada, e force reescreve tudo — comparar conteúdo em vez de data de modificação é o que evita reescrever a árvore inteira toda noite, o que por sua vez faria o indexador do KB reprocessar tudo sem motivo. O estado registra toda página sincronizada, o que é o que torna a passada seguinte incremental de verdade
- intended_status: `not_implemented` · derived health: `not_implemented`
- tests: `repos/aw-app-notion/tests/test_sync.py` (passing)

### Uma página ilegível não aborta a sincronização das outras
- Given uma sincronização que percorre muitas páginas, das quais uma pode falhar por permissão, bloco não suportado ou erro da API
- When o erro daquela página é contido dentro do laço (repos/aw-app-notion/notion_app/sync.py::_render_page:205, exercitado por repos/aw-app-notion/tests/test_sync.py::test_one_unreadable_page_does_not_abort_the_others:144)
- Then as demais páginas seguem sincronizando e só a problemática fica de fora, e root_page_id ausente vira 503 em vez de crash (test_missing_root_page_id_is_a_503_not_a_crash:161) — num job noturno de muitas páginas, abortar na primeira falha significa que uma única página sem permissão congela a base inteira, e o efeito só é notado dias depois, quando alguém busca algo recente e não acha
- intended_status: `not_implemented` · derived health: `not_implemented`
- tests: `repos/aw-app-notion/tests/test_sync.py` (passing)

### O modo pull-only nunca arquiva nada no Notion
- Given a sincronização também sabe empurrar deleções locais para o Notion, arquivando a página cujo .md sumiu
- When a execução roda em pull-only (repos/aw-app-notion/notion_app/sync.py, verificado por repos/aw-app-notion/tests/test_sync.py::test_pull_only_never_archives_anything:174)
- Then nenhuma página é arquivada, aconteça o que acontecer com os arquivos locais — a assimetria é deliberada e importa muito: uma árvore local incompleta (checkout parcial, disco não montado, primeira execução) faria o caminho de push interpretar ausência como deleção e arquivar páginas boas em massa. Puxar errado se conserta puxando de novo; arquivar errado mexe no sistema que é a fonte da verdade
- intended_status: `not_implemented` · derived health: `not_implemented`
- tests: `repos/aw-app-notion/tests/test_sync.py` (passing)

### Salvar o token escreve o mcp.json, e o logout desliga o servidor MCP junto
- Given as tools do Notion só existem no gateway se este app declarar seu upstream em disco, e o token é o que as habilita
- When as settings são salvas ou limpas (repos/aw-app-notion/notion_app/plugin.py, via repos/aw-app-notion/tests/test_routes.py::test_save_settings_writes_secret_and_mcp_json:69 e test_logout_clears_secret_and_disables_mcp_server:99)
- Then salvar grava o segredo E escreve o mcp.json, com a lista de servidores vazia quando não há token (test_build_mcp_servers_empty_without_token:124) e incluindo também o kanban quando há (test_build_mcp_servers_also_advertises_kanban:136), token vazio é recusado, e o endpoint espelha o estado do disco — o app escreve seu próprio mcp.json porque contributes.mcp não registra upstream sozinho; sem essa escrita o app instala, aparece configurado, e o gateway serve zero tools dele
- intended_status: `not_implemented` · derived health: `not_implemented`
- tests: `repos/aw-app-notion/tests/test_routes.py` (passing)
