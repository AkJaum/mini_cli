# Project Runner

Serviço HTTP independente para trabalhar em cópias temporárias de repositórios Git. Um cliente pode preparar um projeto, navegar pelos arquivos, executar `make`, rodar um programa e abrir um terminal interativo. Use a API com `curl`, com um cliente próprio ou pelo servidor de um site.

O Django recebe os pedidos HTTP, o Git cria os clones e o Gunicorn serve a aplicação. O terminal usa um PTY (pseudo-terminal do sistema operacional), que permite enviar teclas, redimensionar a janela e receber a saída conforme o programa roda.

## Configuração

```bash
cp .config.example.json config.json
cp .env.example .env
```

Em `config.json`, cada chave dentro de `projects` é a **ref** (o identificador enviado como `projectId` nas chamadas). Use letras, números, hífen ou sublinhado, com até 64 caracteres. Cadastre seus repositórios e confira os branches antes de iniciar.

```json
{
  "projects": {
    "meu_projeto": {
      "repoUrl": "https://github.com/SEU-USUARIO/SEU-REPOSITORIO.git",
      "branch": "main",
      "runTimeoutMs": 15000,
      "allowBash": true
    }
  }
}
```

| Campo | Uso |
| --- | --- |
| `repoUrl` | URL HTTPS sem credenciais embutidas. Também aceita `file:///caminho/absoluto` para um repositório acessível ao runner, útil para testes locais. |
| `branch` | Branch que o Git vai clonar. O clone traz o commit mais recente desse branch no momento da preparação. |
| `runTimeoutMs` | Opcional. Tempo máximo de `/projects/run` para esse projeto, em milissegundos. O padrão é `RUN_TIMEOUT_MS`. |
| `allowBash` | Opcional. Habilita `runtime: "bash"` no terminal interativo. O padrão é `false` e abrir o Bash exige `RUNNER_SHARED_TOKEN` configurado. |
| `web` | Opcional. Utilizado para projetos que sobem algum site, como algum projeto em Next. Veja [Visualizador web](#visualizador-web). |

O catálogo é lido na inicialização. Reinicie o serviço após editar o JSON. Use `{"projects": {}}` para subir com catálogo vazio. `config.json` e `.env` ficam fora do Git. Configure `DJANGO_SECRET_KEY` e `RUNNER_SHARED_TOKEN` com valores aleatórios e distintos. Você pode gerar cada valor com `python3 -c 'import secrets; print(secrets.token_urlsafe(32))'`.

## Como rodar

### Docker

A imagem inclui Python 3.12, Git, Bash, Make, GCC e G++. Suba a partir da raiz:

```bash
docker build -t project-runner .
docker run --rm --name project-runner \
  --env-file .env \
  --publish 127.0.0.1:8080:8080 \
  --mount "type=bind,src=$PWD/config.json,dst=/app/config.json,readonly" \
  project-runner
```

Deixe `config.json` legível para o usuário do container. Se mudar `PORT`, ajuste o mapeamento de portas. Por padrão, o comando publica a API apenas na própria máquina. Se um site precisar acessá-la, configure a rede privada ou um proxy HTTPS (servidor que repassa as chamadas) e inclua o hostname utilizado em `DJANGO_ALLOWED_HOSTS`.

### Docker no Render

1. Em **Environment → Secret Files**, adicione um arquivo chamado `config.json` com o conteúdo do seu catálogo. O Render disponibiliza o arquivo em `/etc/secrets/config.json` quando o container inicia. [Arquivos secretos no Render](https://render.com/docs/docker-secrets#accessing-secret-files-at-runtime)
2. Em **Environment Variables**, defina `PROJECT_CONFIG_PATH=/etc/secrets/config.json` e inclua o domínio do serviço em `DJANGO_ALLOWED_HOSTS` (por exemplo, `mini-cli.onrender.com`). Defina valores aleatórios e distintos para `DJANGO_SECRET_KEY` e `RUNNER_SHARED_TOKEN`. O Gunicorn usa a porta fornecida pelo Render em `PORT`. [Porta do serviço](https://render.com/docs/web-services#port-binding)
3. Em **Settings → Health Checks**, configure `/health` e faça o deploy. A rota deve responder com HTTP 200 e `"ok": true`. [Health Checks do Render](https://render.com/docs/health-checks)

### Python local

Requer Linux ou macOS, Python 3.12, Git, Bash e Make. Instale os compiladores exigidos pelos projetos que for executar.

```bash
make install
make run
```

`make install` cria o `.venv` (ambiente virtual do Python) com Python 3.12 quando necessário e instala as dependências nele. `make run` e `make test` usam esse ambiente diretamente, sem precisar ativá-lo no terminal.

`make run` carrega o `.env` quando presente e inicia o Gunicorn na porta definida em `PORT` (padrão 8080). `make test` roda os testes do Django.

`make clean` remove o `.venv` e os caches locais de Python, Ruff e pytest. Preserva `.env`, `config.json` e os clones. Rode `make install` novamente para recriar o ambiente.

### Variáveis de ambiente

| Variável | Padrão e uso |
| --- | --- |
| `PORT` | `8080`. Porta do serviço. |
| `WORKDIR_BASE` | `/tmp/runner-workspaces`. Diretório gravável onde os clones ficam. |
| `PROJECT_CONFIG_PATH` | Opcional. Local do catálogo JSON. O padrão é `config.json` na raiz. Caminhos relativos são resolvidos a partir da raiz do projeto. |
| `DJANGO_SECRET_KEY` | Segredo do Django. Defina um valor próprio em qualquer ambiente compartilhado. |
| `DJANGO_ALLOWED_HOSTS` | `localhost,127.0.0.1,project-runner`. Hosts separados por vírgula. |
| `RUNNER_SHARED_TOKEN` | Token que o cliente envia em `X-Runner-Token`. Sem ele, as rotas comuns ficam abertas e a abertura do Bash fica desabilitada. |
| `DATA_UPLOAD_MAX_MEMORY_SIZE` | `262144`. Limite em bytes do corpo da chamada HTTP. |
| `MAX_FILE_SIZE_BYTES` | `8388608`. Tamanho máximo de um arquivo para leitura pela API. |
| `MAX_OUTPUT_BYTES` | `262144`. Limite em bytes da saída devolvida por `build` e `run`, além do aviso de truncamento. |
| `CLONE_TIMEOUT_MS`, `BUILD_TIMEOUT_MS`, `RUN_TIMEOUT_MS` | `30000`, `30000`, `10000`. Tempos máximos em milissegundos. |
| `MAX_BUILD_ARGS`, `MAX_RUN_ARGS` | `8`, `32`. O build rejeita argumentos excedentes. A execução usa até o limite configurado. |
| `MAX_REQUESTS_PER_ID_PER_MINUTE`, `RATE_WINDOW_SECONDS` | `120`, `60`. Número de chamadas por identificador e duração da janela. |
| `MAX_CONCURRENT_REQUESTS_PER_ID`, `MAX_GLOBAL_CONCURRENT_REQUESTS` | `6`, `64`. Limites de operações simultâneas por identificador e por processo Gunicorn. |
| `PENDING_REQUEST_TTL_SECONDS` | `30`. Prazo para retirar chamadas antigas da contagem de concorrência. |
| `MAX_PTY_INPUT_BYTES`, `MAX_PTY_BUFFER_BYTES` | `4096`, `524288`. Limites em bytes por envio de teclas e da saída do terminal aguardando leitura. |
| `PTY_MAX_LIFETIME_SECONDS` | `300`. Tempo máximo da sessão interativa. |

## API

Com `RUNNER_SHARED_TOKEN` configurado, todas as rotas abaixo, exceto `/health`, usam `X-Runner-Token`. As rotas com corpo recebem `Content-Type: application/json`. Use `X-Client-Id` por cliente quando a integração precisar agrupar os limites de requisições de forma consistente.

| Método e rota | Campos e resposta |
| --- | --- |
| `GET /health` | Status básico e horário da API. |
| `GET /projects` | Objeto com `projects`, a lista de refs do catálogo. |
| `POST /projects/prepare` | Prepara um clone e devolve `workdir` e os itens da raiz. |
| `POST /projects/fs/list` | `path` (opcional). Lista nomes e tipos dos itens de um diretório. |
| `POST /projects/fs/read` | `file` e `path` (opcional). Retorna texto UTF-8 ou PDF em Base64 (bytes representados como texto). |
| `POST /projects/build` | `args` e `path` (opcionais). Executa `make` e devolve `success`, `exitCode`, `timedOut` e `output`. |
| `POST /projects/run` | `executable`, `runtime` (`native` ou `python`), `path`, `args` e `stdin`. Só `executable` é obrigatório. Retorna os mesmos campos de execução de `build`. |
| `POST /projects/process/start` | Aceita `path`, `runtime`, `rows` e `cols`. Use `executable` e, opcionalmente, `args` nos modos `native` e `python`. Para abrir o Bash, envie `runtime: "bash"`. |
| `POST /projects/process/poll` | Consome a saída acumulada em `outputBase64`. Retorna também `running`, `exitCode`, `droppedOutput` e `expired`. |
| `POST /projects/process/input` | `data` com o texto ou as teclas a enviar. Inclua `\n` para Enter. |
| `POST /projects/process/resize` | `rows` de 12 a 120 e `cols` de 40 a 240. Os padrões são 40 e 100. |
| `DELETE /projects/cleanup` | Encerra o terminal e remove o clone. Clientes antigos também podem enviar o `workdir` exato devolvido na preparação. |

As rotas de projeto com corpo recebem `projectId` e aceitam um `sessionId` de 8 a 128 caracteres (letras, números, hífen e sublinhado). Use a mesma combinação em todas as chamadas de um terminal. Sessões diferentes da mesma ref têm clones separados. Chamadas sem `sessionId` compartilham o clone legado daquela ref. `path` usa uma lista de partes relativas à raiz, como `["src", "utils"]`.

### Abrir um Bash e executar um comando

Configure uma ref chamada `meu_projeto` com `"allowBash": true` e tenha `curl` e `jq` (ferramenta para ler JSON no terminal) disponíveis.

```bash
source ./.env

runner() {
  curl -sS --fail-with-body \
    -H "X-Runner-Token: $RUNNER_SHARED_TOKEN" \
    -H "Content-Type: application/json" "$@"
}

runner "http://127.0.0.1:$PORT/projects"
runner -d '{"projectId":"meu_projeto","sessionId":"sessao_demo_01","runtime":"bash"}' \
  "http://127.0.0.1:$PORT/projects/process/start"
runner -d '{"projectId":"meu_projeto","sessionId":"sessao_demo_01","data":"pwd\nexit\n"}' \
  "http://127.0.0.1:$PORT/projects/process/input"
while true; do
  result=$(runner -d '{"projectId":"meu_projeto","sessionId":"sessao_demo_01"}' \
    "http://127.0.0.1:$PORT/projects/process/poll") || break
  jq -j '.outputBase64 | @base64d' <<< "$result"
  [ "$(jq -r '.running' <<< "$result")" = true ] || break
  sleep 0.2
done
runner -X DELETE -d '{"projectId":"meu_projeto","sessionId":"sessao_demo_01"}' \
  "http://127.0.0.1:$PORT/projects/cleanup"
```

O cliente chama `poll` repetidamente até receber `running: false` e decodifica cada `outputBase64` antes de exibir a saída. A consulta final consome a sessão e as próximas retornam 404. Chame `cleanup` ao terminar para liberar o clone.

### Visualizador web

Para um projeto com `web` no catálogo, `POST /projects/build` com `"args": ["web"]` inicia `web.command` na raiz do clone. O runner espera a aplicação responder em `http://127.0.0.1:<web.port>` e devolve `web.url` como `webUrl`. Configure a URL alcançável pelo cliente e publique ou encaminhe essa porta separadamente. O processo é compartilhado pela ref enquanto está ativo e o cleanup preserva o clone que o hospeda. O exemplo do Fly-in usa NiceGUI.

## Integração e estado do serviço

O servidor do site guarda o token, autentica seus usuários e encaminha ao runner somente as operações que eles podem executar. Para oferecer um programa interativo específico, abra-o usando `runtime: "native"` ou `runtime: "python"` e envie as teclas pelas rotas de processo.

Clones, sessões e limites de requisições são associados a um processo da API. Use uma instância Gunicorn com um worker (processo servidor) e múltiplas threads (chamadas simultâneas dentro dele). Reiniciar perde as sessões em memória. Em Docker, `--rm` remove o sistema de arquivos do container ao parar. Numa execução local, clones remanescentes podem continuar em `WORKDIR_BASE`.

O diretório de trabalho separa as cópias dos projetos. Os comandos rodam com as permissões do usuário do runner. Use repositórios confiáveis e execute o serviço dedicado com recursos limitados, sem montar credenciais ou diretórios sensíveis. Restrinja a API e o Bash a clientes que podem executar código nesse ambiente.

## Verificação

Depois de rodar `make install` e criar `config.json`:

```bash
.venv/bin/python manage.py check
make test
.venv/bin/python -m pip install ruff
.venv/bin/ruff check .
```

Os testes criam um repositório temporário local e exercitam o fluxo HTTP de clone, arquivos, build, execução e terminal sem depender da rede.
