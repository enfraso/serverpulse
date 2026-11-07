# ServerPulse

Painel web leve para monitorar um servidor Linux, inspirado no btop. O backend usa Python 3 e a biblioteca padrão; não requer pip, Node.js nem banco de dados externo. O histórico é armazenado em SQLite.

## Recursos

- CPU, temperaturas, RAM, swap, volumes, rede, uptime, load average e processos.
- Histórico persistido de CPU, RAM e rede, com gráficos de 24 horas e 7 dias e picos do período.
- Alertas no painel para temperatura alta, memória elevada, serviços systemd com falha, containers parados e alvos TCP indisponíveis.
- Lista de serviços systemd e containers Docker, quando as ferramentas e permissões estão disponíveis.
- Portas TCP em escuta e quantidade de conexões estabelecidas.
- Pesquisa por processos exibidos, serviços, containers, portas e verificações TCP.

## Requisitos

- Linux com `/proc`, Python 3.9+ e systemd (para instalação como serviço).
- Docker instalado e permissões de acesso ao daemon para exibir containers (opcional).
- Caddy já instalado (opcional, recomendado para HTTPS).

## Instalação no Debian 13 / DietPi

Clone o repositório no servidor:

```bash
git clone https://github.com/enfraso/serverpulse.git
cd serverpulse
sudo bash install.sh
```

O serviço escuta em `127.0.0.1:8764`. O histórico persistente fica em `/var/lib/serverpulse/history.sqlite3` e é amostrado a cada 60 segundos, com retenção de 8 dias. O usuário do serviço precisa de acesso de escrita a esse diretório; `StateDirectory` do systemd o prepara automaticamente.

Verifique:

```bash
sudo systemctl status serverpulse
curl http://127.0.0.1:8764/api/health
curl -s http://127.0.0.1:8764/api/metrics | python3 -m json.tool | head -50
curl -s 'http://127.0.0.1:8764/api/history?period=24h' | python3 -m json.tool
```

Não altere `SERVERPULSE_HOST` para `0.0.0.0` sem configurar firewall e autenticação apropriados.

## Verificações de disponibilidade

Configure opcionalmente alvos TCP por variável de ambiente no serviço systemd:

```ini
Environment="SERVERPULSE_CHECKS=web=127.0.0.1:8080,db=10.0.0.12:5432"
```

Até 20 alvos são testados com timeout curto. IPv6 pode ser escrito entre colchetes, por exemplo `local=[::1]:8080`. Falhas aparecem como alertas no painel.

## Docker sem dar acesso administrativo ao ServerPulse

Por padrão, o usuário `serverpulse` não acessa `/var/run/docker.sock`. Não o adicione ao grupo `docker`: esse grupo normalmente pode controlar o daemon e obter privilégios equivalentes aos de root.

Se quiser exibir containers sem dar acesso direto ao socket ao ServerPulse, pode usar um proxy do Docker API com operações de escrita desabilitadas. O exemplo abaixo usa o `tecnativa/docker-socket-proxy`; avalie e mantenha essa imagem atualizada conforme sua política de segurança:

```bash
sudo docker run -d \
  --name serverpulse-docker-proxy \
  --restart unless-stopped \
  -p 127.0.0.1:2375:2375 \
  -v /var/run/docker.sock:/var/run/docker.sock:ro \
  -e PING=1 \
  -e VERSION=1 \
  -e CONTAINERS=1 \
  -e POST=0 \
  tecnativa/docker-socket-proxy
```

A porta `2375` fica vinculada apenas ao loopback. `CONTAINERS=1` libera a consulta de containers e `POST=0` bloqueia chamadas HTTP de escrita. O proxy, por precisar ler o socket, continua sendo um componente privilegiado; mantenha-o atualizado e não publique essa porta na rede.

Configure o serviço ServerPulse para usar o proxy:

```bash
sudo systemctl edit serverpulse
```

Adicione ao drop-in:

```ini
[Service]
Environment=DOCKER_HOST=tcp://127.0.0.1:2375
```

Então reinicie e confira o resultado:

```bash
sudo systemctl daemon-reload
sudo systemctl restart serverpulse
curl -s http://127.0.0.1:8764/api/metrics | python3 -c 'import json,sys; print(json.load(sys.stdin)["docker"])'
```

O ServerPulse usa o cliente Docker instalado no host e herda `DOCKER_HOST` do serviço. Se o Docker usar outro caminho de socket, ajuste o volume do proxy. Para remover a integração, apague o drop-in em `/etc/systemd/system/serverpulse.service.d/` e reinicie o serviço.

## Publicar pelo Caddy

Crie um subdomínio DNS apontando para seu servidor, por exemplo `monitor.seudominio.com`, e adicione ao Caddyfile:

```caddyfile
monitor.seudominio.com {
    basic_auth {
        SEU_USUARIO HASH_GERADO_PELO_CADDY
    }
    reverse_proxy 127.0.0.1:8764
}
```

Gere o hash de senha no servidor:

```bash
caddy hash-password --plaintext 'USE_UMA_SENHA_FORTE_E_UNICA'
```

Substitua `SEU_USUARIO` e `HASH_GERADO_PELO_CADDY` pelo usuário escolhido e pelo hash retornado. Não coloque a senha em texto puro no Caddyfile.

Valide e recarregue o Caddy:

```bash
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

O Caddy cuida do HTTPS e do Basic Auth; a API continua escutando somente no loopback. Incorpore o bloco no local adequado do Caddyfile sem apagar os sites existentes.

## Configuração

O serviço lê as seguintes variáveis de ambiente:

- `SERVERPULSE_HOST`: endereço de escuta (padrão `127.0.0.1`).
- `SERVERPULSE_PORT`: porta HTTP (padrão `8764`).
- `SERVERPULSE_DATA_DIR`: diretório do histórico (padrão `/var/lib/serverpulse`).
- `SERVERPULSE_HISTORY_INTERVAL`: intervalo de amostragem em segundos (padrão `60`, mínimo `15`).
- `SERVERPULSE_TEMP_WARN_C`: limite de alerta térmico em °C (padrão `80`).
- `SERVERPULSE_MEMORY_WARN_PERCENT`: limite de alerta de memória em percentual (padrão `90`).
- `SERVERPULSE_CHECKS`: lista opcional de verificações TCP `nome=host:porta`, separadas por vírgula.
- `SERVERPULSE_REFRESH`: intervalo aproximado para estimar CPU de processos (padrão `1`).
- `SERVERPULSE_INTERFACE`: vazio para agregar interfaces não-loopback ou um nome específico, como `eth0`.

Para mudar uma variável no serviço, edite `/etc/systemd/system/serverpulse.service` e aplique:

```bash
sudo systemctl daemon-reload
sudo systemctl restart serverpulse
```

## API

- `GET /api/health`: estado e versão.
- `GET /api/metrics`: métricas em tempo real, serviços, containers, conexões e alertas.
- `GET /api/history?period=24h` ou `period=7d`: pontos agrupados e picos registrados.
- `GET /api/search?q=termo`: pesquisa nos itens monitorados.

## Desenvolvimento local

```bash
python3 serverpulse.py
```

Abra `http://127.0.0.1:8764`. Defina `SERVERPULSE_DATA_DIR` para um diretório gravável no ambiente local se necessário.

## Segurança e limitações

- O backend escuta em `127.0.0.1`; proteja acesso externo com HTTPS e autenticação e não exponha a porta diretamente na internet.
- A leitura de serviços systemd, containers Docker e processos depende das permissões do usuário do serviço. O acesso ao socket Docker concede privilégios elevados; conceda-o somente se necessário.
- Os alertas são exibidos no painel, sem envio por e-mail ou notificações externas.
- O monitoramento TCP mostra portas em escuta e contagem de conexões estabelecidas; não associa sockets a processos.
- Temperaturas dependem do driver/kernel e podem não estar disponíveis em todo hardware.
- A lista de processos exibida e pesquisável contém os 12 principais por CPU/memória.
- O percentual de CPU de processos é uma estimativa e pode variar.
