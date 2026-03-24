# Homelab Monitor

## Português (PT-BR)

Painel web para monitoramento de host Linux e gerenciamento de Docker/Compose em tempo real.

### Visão Geral

O projeto combina:

- Backend em FastAPI (`main.py`)
- Frontend React servido como arquivo estático (`static/index.html`)
- WebSockets para métricas, logs e terminal interativo

Funcionalidades principais:

- Monitoramento de CPU, RAM, disco, rede e uptime
- Listagem e ações em containers (start, stop, restart, remove, pause, unpause)
- Streaming de logs em tempo real
- Terminal interativo dentro do container (`docker exec` via WebSocket)
- Descoberta e ações de projetos Docker Compose (`up`, `down`, `pull`, `rebuild`)

### Requisitos

- Linux (recomendado) com Docker ativo
- Acesso ao socket Docker (`/var/run/docker.sock`)
- Python 3.12+ (se rodar sem container)
- Docker CLI + plugin `docker compose` (necessário para endpoints de Compose)

Observação:

- O projeto foi desenhado para ambiente Linux/homelab. Em Windows/macOS, use preferencialmente WSL2/Linux VM para métricas de host mais corretas.

### Estrutura do Projeto

- `main.py`: API REST + WebSocket + serviço de arquivos estáticos
- `static/index.html`: frontend do painel
- `docker-compose.yml`: execução containerizada
- `Dockerfile`: imagem da aplicação
- `requirements.txt`: dependências Python

### Como Rodar

#### Opção 1: Docker Compose (recomendada)

```bash
docker compose up -d --build
```

Interface:

- `http://localhost:9090`

#### Opção 2: Execução local (sem container)

Linux/macOS:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

PowerShell:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python main.py
```

Interface:

- `http://localhost:9090`

### Configuração para Docker Compose externo

Para que a aba `COMPOSE` consiga executar ações em projetos fora deste repositório, monte os diretórios desses projetos no container em `docker-compose.yml` (seção `volumes`), por exemplo:

```yaml
# - /home/user/minecraft-server:/projects/minecraft:ro
# - /home/user/other-project:/projects/other:ro
```

Importante:

- O endpoint `/api/compose` valida se existe `docker-compose.yml|yaml` ou `compose.yml|yaml` no `project_dir`.
- A descoberta de projetos usa labels `com.docker.compose.*` dos containers.

### Endpoints REST

- `GET /` -> frontend
- `GET /api/system` -> métricas detalhadas do host
- `GET /api/containers?all=true` -> lista containers
- `GET /api/containers/{container_id}/stats` -> stats de um container
- `POST /api/containers/{container_id}` -> ação em container
- `GET /api/containers/{container_id}/logs?tail=200` -> logs recentes
- `GET /api/compose` -> projetos Compose detectados
- `POST /api/compose` -> ação Compose (`up|down|pull|rebuild|ps`)
- `GET /api/docker/info` -> informações do daemon Docker
- `GET /api/images` -> lista imagens
- `DELETE /api/images/{image_id}` -> remove imagem
- `GET /api/metrics/history` -> histórico de métricas em memória
- `GET /api/actions?limit=50` -> histórico de ações

Exemplo de ação em container:

```json
{
  "action": "restart"
}
```

Exemplo de ação em compose:

```json
{
  "action": "up",
  "project_dir": "/home/user/meu-projeto",
  "service": null
}
```

### Endpoints WebSocket

- `/ws/metrics` -> stream de métricas do host (2s)
- `/ws/logs/{container_id}` -> stream de logs em tempo real
- `/ws/exec/{container_id}` -> terminal interativo no container

### Segurança

Este projeto oferece controle administrativo de Docker e atualmente não tem autenticação/controle de acesso nativo.

Recomendações:

- Expor apenas em rede confiável
- Proteger com reverse proxy + autenticação (ex.: Authelia, OAuth2 Proxy, Traefik ForwardAuth)
- Restringir acesso por firewall/VLAN

### Troubleshooting

- `Docker connection failed` no startup: verifique se o daemon Docker está rodando e se há permissão de acesso ao socket Docker.
- Aba `COMPOSE` vazia: garanta que os containers tenham labels de Compose e que os diretórios dos projetos estejam montados.
- Erro `No compose file found`: verifique se existe arquivo compose no `project_dir`.
- Terminal não conecta: o container precisa estar em estado `running`.

### Desenvolvimento

Para alterar frontend:

- Edite `static/index.html`
- Recarregue a página no navegador

Para alterar backend:

- Edite `main.py`
- Reinicie a aplicação

---

## English

Web panel for Linux host monitoring and real-time Docker/Compose management.

### Overview

This project combines:

- FastAPI backend (`main.py`)
- React frontend served as a static file (`static/index.html`)
- WebSockets for metrics, logs, and interactive terminal

Main features:

- CPU, RAM, disk, network, and uptime monitoring
- Container listing and actions (start, stop, restart, remove, pause, unpause)
- Real-time log streaming
- Interactive shell inside containers (`docker exec` over WebSocket)
- Docker Compose project discovery and actions (`up`, `down`, `pull`, `rebuild`)

### Requirements

- Linux (recommended) with Docker running
- Access to Docker socket (`/var/run/docker.sock`)
- Python 3.12+ (if running without container)
- Docker CLI + `docker compose` plugin (required for Compose endpoints)

Note:

- The project is designed for Linux/homelab environments. On Windows/macOS, prefer WSL2/Linux VM for accurate host metrics.

### Project Structure

- `main.py`: REST API + WebSocket + static file serving
- `static/index.html`: dashboard frontend
- `docker-compose.yml`: containerized runtime
- `Dockerfile`: application image
- `requirements.txt`: Python dependencies

### Running the Project

#### Option 1: Docker Compose (recommended)

```bash
docker compose up -d --build
```

Access:

- `http://localhost:9090`

#### Option 2: Local run (without container)

Linux/macOS:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

PowerShell:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python main.py
```

Access:

- `http://localhost:9090`

### External Docker Compose Configuration

To allow the `COMPOSE` panel to run actions on projects outside this repository, mount those project directories into the container in `docker-compose.yml` (`volumes` section), for example:

```yaml
# - /home/user/minecraft-server:/projects/minecraft:ro
# - /home/user/other-project:/projects/other:ro
```

Important:

- `/api/compose` validates whether `docker-compose.yml|yaml` or `compose.yml|yaml` exists in `project_dir`.
- Project discovery relies on `com.docker.compose.*` labels from containers.

### REST Endpoints

- `GET /` -> frontend
- `GET /api/system` -> detailed host metrics
- `GET /api/containers?all=true` -> list containers
- `GET /api/containers/{container_id}/stats` -> container stats
- `POST /api/containers/{container_id}` -> container action
- `GET /api/containers/{container_id}/logs?tail=200` -> recent logs
- `GET /api/compose` -> detected Compose projects
- `POST /api/compose` -> Compose action (`up|down|pull|rebuild|ps`)
- `GET /api/docker/info` -> Docker daemon info
- `GET /api/images` -> list images
- `DELETE /api/images/{image_id}` -> remove image
- `GET /api/metrics/history` -> in-memory metrics history
- `GET /api/actions?limit=50` -> action history

Container action example:

```json
{
  "action": "restart"
}
```

Compose action example:

```json
{
  "action": "up",
  "project_dir": "/home/user/my-project",
  "service": null
}
```

### WebSocket Endpoints

- `/ws/metrics` -> host metrics stream (2s)
- `/ws/logs/{container_id}` -> real-time logs stream
- `/ws/exec/{container_id}` -> interactive terminal in container

### Security

This project provides administrative Docker control and currently does not include native authentication/access control.

Recommendations:

- Expose only on trusted networks
- Protect with reverse proxy + authentication (e.g. Authelia, OAuth2 Proxy, Traefik ForwardAuth)
- Restrict access with firewall/VLAN

### Troubleshooting

- `Docker connection failed` at startup: check whether Docker daemon is running and whether socket permissions are correct.
- Empty `COMPOSE` panel: ensure containers include Compose labels and project directories are mounted.
- `No compose file found` error: check whether a compose file exists in `project_dir`.
- Terminal does not connect: container must be in `running` state.

### Development

For frontend changes:

- Edit `static/index.html`
- Refresh browser page

For backend changes:

- Edit `main.py`
- Restart the app
