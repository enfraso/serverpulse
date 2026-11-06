# ServerPulse

Painel web leve para monitorar um servidor Linux, inspirado no btop. Backend em Python 3 usando apenas a biblioteca padrão. Não requer pip, Docker, Node.js ou banco de dados.

<img width="1871" height="857" alt="image" src="https://github.com/user-attachments/assets/f84d665a-935b-4604-9f7c-727d65eeda3d" />


## Métricas

- CPU (amostragem entre requisições), quantidade de CPUs e temperaturas quando expostas em `/sys/class/thermal`
- RAM e swap via `/proc/meminfo`
- Uso dos sistemas de arquivos locais
- Taxas e totais de rede via `/proc/net/dev`
- Load average, uptime, hostname, kernel e arquitetura
- Processos com PID, nome, usuário, CPU aproximada e RSS

A API lê `/proc` e `/sys` a cada chamada. A interface consulta `/api/metrics` a cada segundo.

## Requisitos

- Linux com `/proc` e Python 3.9+
- systemd (para instalação como serviço)
- Caddy já instalado (opcional, recomendado para HTTPS)

## Instalação no Debian 13 / DietPi

Clone o repositório no servidor:

```bash
git clone https://github.com/servepulse/serverpulse.git
cd serverpulse
sudo bash install.sh
```

Verifique:

```bash
sudo systemctl status serverpulse
curl http://127.0.0.1:8765/api/health
curl -s http://127.0.0.1:8765/api/metrics | python3 -m json.tool | head -50
```

A aplicação fica em `http://127.0.0.1:8765`, acessível localmente no próprio host. **Não altere `SERVERPULSE_HOST` para `0.0.0.0`** sem configurar firewall e autenticação apropriados.

## Publicar pelo Caddy

Crie um subdomínio DNS apontando para seu servidor, por exemplo `monitor.seudominio.com`, e adicione ao Caddyfile:

```caddyfile
monitor.seudominio.com {
    basic_auth {
        SEU_USUARIO HASH_GERADO_PELO_CADDY
    }
    reverse_proxy 127.0.0.1:8765
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

Acesse `https://monitor.seudominio.com`. O Caddy cuida do HTTPS e do Basic Auth; a API continua escutando somente no loopback. Se seu Caddyfile usa outro formato de organização, incorpore o bloco no local adequado sem apagar os sites existentes.

## Ajustes opcionais

O serviço lê variáveis de ambiente:

- `SERVERPULSE_HOST` (padrão `127.0.0.1`)
- `SERVERPULSE_PORT` (padrão `8765`)
- `SERVERPULSE_REFRESH` (padrão `1`, intervalo usado para estimar CPU de processos)
- `SERVERPULSE_INTERFACE` (vazio = soma interfaces não-loopback; ou defina, por exemplo, `eth0`)

Para mudar a interface, edite `/etc/systemd/system/serverpulse.service`, adicione uma linha `Environment=SERVERPULSE_INTERFACE=eth0` e execute:

```bash
sudo systemctl daemon-reload
sudo systemctl restart serverpulse
```

## Desenvolvimento local

```bash
python3 serverpulse.py
```

Abra `http://127.0.0.1:8765`.

## Segurança

- O backend escuta em `127.0.0.1`, não diretamente na internet.
- Proteja o acesso externo com Caddy `basic_auth` e HTTPS.
- Não exponha a porta 8765 no roteador/firewall.
- O usuário do serviço é dedicado e sem shell de login.
- A lista de processos e métricas pode revelar nomes de serviços e carga do host; mantenha autenticação ativa.
- O painel é somente leitura: não executa comandos administrativos nem permite encerrar processos.

## Limitações

- Temperatura depende do driver/kernel e pode não estar disponível em todo hardware (especialmente algumas placas ARM).
- O percentual de CPU do processo é uma estimativa baseada no intervalo de consulta e pode variar.
- Dispositivos de rede são agregados por padrão, excluindo `lo`; filtre com `SERVERPULSE_INTERFACE`.
- É uma ferramenta de monitoramento leve, não substitui Prometheus, Netdata ou ferramentas de alerta.
