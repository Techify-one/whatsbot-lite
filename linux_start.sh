#!/bin/bash
# WhatsBot-Lite — Linux native launcher.
#
# Equivalente do ``windows_start.bat --server`` no Windows: roda Python local
# (sem Docker) com ``uvicorn`` watchando core + plugins. Edita qualquer ``.py``
# em ``server/``, ``agent/``, ``config/``, ``gowa/``, ``db/``, ``plugins/`` ou
# ``storages/plugins/`` e o worker reinicia sozinho.
#
# O loop externo cobre quando o uvicorn morre (raro) ou um plugin chama
# ``os._exit`` (enable/disable via UI dispara ``schedule_restart``).
#
# Pra rodar em modo Docker (prod-like) use ``./docker_start.sh``.
set -u

# Porta web (frontend + REST + WS). Pode ser sobrescrita externamente.
export WHATSBOT_WEB_PORT="${WHATSBOT_WEB_PORT:-8090}"
# Porta interna do subprocess GOWA (não exposta).
export WHATSBOT_GOWA_PORT="${WHATSBOT_GOWA_PORT:-64998}"

# Hot-reload. Default 1 (dev). EM PRODUÇÃO rode com ``WHATSBOT_RELOAD=0``.
#
# Por quê: ``--reload`` faz o uvicorn subir DOIS processos — um pai (reloader,
# dono do socket da porta) e um worker (que roda o app). O ``os._exit(0)`` que
# ``plugins.restart.schedule_restart()`` dispara ao ativar/desativar um plugin
# mata só o WORKER, e o pai NÃO recria worker que morreu sozinho: ele segue
# vivo segurando a porta em LISTEN sem ninguém atendendo. Como o uvicorn nunca
# "sai", nem o loop abaixo nem o ``Restart=always`` do systemd percebem —
# ``systemctl status`` mostra ``active`` com o app morto, e só um restart
# manual recupera.
#
# Além disso ``storages/plugins`` é um ``--reload-dir``: instalar um plugin
# extrai o zip dentro dele, o watcher dispara e o worker é reiniciado NO MEIO
# da requisição de upload, fazendo o import falhar.
#
# Com ``WHATSBOT_RELOAD=0`` o uvicorn é processo único: o ``os._exit(0)``
# encerra o processo de verdade, o loop abaixo relança em ~2s, e escrever
# arquivos em ``storages/plugins`` deixa de reiniciar nada.
WHATSBOT_RELOAD="${WHATSBOT_RELOAD:-1}"

cd "$(dirname "$0")"

# ===== Serviço systemd (1 instância por servidor; Docker/Coolify não usa isto) =====
#   ./linux_start.sh service install     cria /etc/systemd/system/whatsbot-lite.service, liga no boot e inicia
#   ./linux_start.sh service uninstall   para e remove (dados em storages/ ficam intactos)
#   ./linux_start.sh service status|logs|print   (print só imprime a unit, sem sudo)
# Portas: WHATSBOT_WEB_PORT=8080 ./linux_start.sh service install
if [ "${1:-}" = "service" ]; then
    set -e
    SERVICE="whatsbot-lite"
    UNIT="/etc/systemd/system/${SERVICE}.service"
    DIR="$(pwd)"
    # Roda como o DONO da pasta: self-update, pip e storages/ escrevem ali. Root deixaria
    # arquivos que o ./linux_start.sh manual não consegue mais alterar.
    OWNER="$(stat -c %U "$DIR")"

    unit_text() {
        cat <<EOF
[Unit]
Description=WhatsBot-Lite
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${OWNER}
WorkingDirectory=${DIR}
# SERVICE=1: este script faz exec do uvicorn (sem loop), o Restart= refaz o script inteiro.
# RELOAD=0: com --reload o os._exit de plugin/update mata só o worker e o serviço fica "ativo" morto.
Environment=WHATSBOT_SERVICE=1
Environment=WHATSBOT_RELOAD=0
Environment=WHATSBOT_WEB_PORT=${WHATSBOT_WEB_PORT}
Environment=WHATSBOT_GOWA_PORT=${WHATSBOT_GOWA_PORT}
ExecStart=/bin/bash "${DIR}/linux_start.sh"
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
    }

    as_root() {
        [ "$(id -u)" -eq 0 ] && return 0
        # ``sudo env`` repassa as portas escolhidas (o sudo limpa o ambiente).
        exec sudo env "WHATSBOT_WEB_PORT=$WHATSBOT_WEB_PORT" "WHATSBOT_GOWA_PORT=$WHATSBOT_GOWA_PORT" /bin/bash "$DIR/linux_start.sh" "$@"
    }

    # O start mata QUALQUER processo na porta (free_port): outra instância nela seria derrubada.
    port_busy() { ss -ltnH "sport = :$1" 2>/dev/null | grep -q .; }

    # O nome ``whatsbot-lite`` é fixo. Se já existe uma unit de OUTRA pasta (ex.: outra cópia do repo na
    # mesma máquina), instalar/remover aqui a sobrescreveria ou apagaria. Só mexe na que é desta pasta.
    foreign_unit() {
        [ -f "$UNIT" ] || return 1
        [ "$(systemctl show -p WorkingDirectory --value "$SERVICE" 2>/dev/null)" != "$DIR" ]
    }

    case "${2:-}" in
        print)
            unit_text
            ;;
        install)
            as_root "$@"
            if [ "$OWNER" = "root" ]; then
                echo "[service] ERRO: a pasta $DIR pertence ao root. Rode como o dono (chown -R usuario:usuario) e tente de novo." >&2
                exit 1
            fi
            if foreign_unit; then
                echo "[service] ERRO: já existe o serviço '$SERVICE' apontando para outra pasta ($(systemctl show -p WorkingDirectory --value "$SERVICE"))." >&2
                echo "          Instalar aqui o sobrescreveria. Um serviço por máquina: remova o outro antes, ou rode esta cópia só no terminal (opção 2)." >&2
                exit 1
            fi
            if ! systemctl is-active --quiet "$SERVICE"; then
                for p in "$WHATSBOT_WEB_PORT" "$WHATSBOT_GOWA_PORT"; do
                    if port_busy "$p"; then
                        echo "[service] ERRO: a porta $p já está em uso (o start do WhatsBot-Lite derrubaria esse processo)." >&2
                        echo "          Escolha outra: WHATSBOT_WEB_PORT=8080 WHATSBOT_GOWA_PORT=64999 ./linux_start.sh service install" >&2
                        exit 1
                    fi
                done
            fi
            unit_text > "$UNIT"
            systemctl daemon-reload
            systemctl enable "$SERVICE"
            systemctl restart "$SERVICE"
            echo "[service] instalado como '$OWNER'. Painel: http://localhost:${WHATSBOT_WEB_PORT}"
            echo "          Na 1ª subida ele cria a venv e baixa dependências/GOWA: pode levar alguns minutos."
            echo "          logs: bash linux_start.sh service logs   |   status: bash linux_start.sh service status"
            ;;
        uninstall)
            as_root "$@"
            if foreign_unit; then
                echo "[service] ERRO: o serviço '$SERVICE' pertence a outra pasta ($(systemctl show -p WorkingDirectory --value "$SERVICE")); não removo daqui." >&2
                exit 1
            fi
            systemctl disable --now "$SERVICE" 2>/dev/null || true
            rm -f "$UNIT" "$DIR/storages/linux_service_choice.txt"
            systemctl daemon-reload
            echo "[service] removido. Os dados em $DIR/storages não foram tocados; a pergunta 'serviço ou terminal' volta no próximo bash linux_start.sh."
            ;;
        status)
            systemctl status "$SERVICE" --no-pager || true
            ;;
        logs)
            as_root "$@"
            exec journalctl -u "$SERVICE" -n 100 -f
            ;;
        *)
            echo "uso: ./linux_start.sh service install|uninstall|status|logs|print" >&2
            exit 1
            ;;
    esac
    exit 0
fi

# ===== 1ª execução: serviço ou só terminal (equivalente ao windows_start.bat) =====
# Só a escolha "somente terminal" é gravada em storages/linux_service_choice.txt (como o
# windows_autostart_choice.txt); "serviço" é lembrado pelo próprio systemd. O uninstall apaga o arquivo.
# NUNCA roda dentro do próprio serviço (WHATSBOT_SERVICE=1): ele cairia no ``exit 0`` abaixo e o
# Restart=always giraria em loop. Também só pergunta com terminal interativo (-t 0).
CHOICE_FILE="storages/linux_service_choice.txt"
if [ "${WHATSBOT_SERVICE:-0}" != "1" ]; then
    choice="$(cat "$CHOICE_FILE" 2>/dev/null || true)"
    # Já é serviço DESTA pasta: subir à mão brigaria pela porta (o free_port abaixo mataria o serviço).
    # Não depende do arquivo de escolha: cobre também quem rodou ``service install`` direto.
    if systemctl is-enabled --quiet whatsbot-lite 2>/dev/null \
        && [ "$(systemctl show -p WorkingDirectory --value whatsbot-lite 2>/dev/null)" = "$(pwd)" ]; then
        echo "[linux_start] O WhatsBot-Lite já está instalado como serviço (roda sozinho no boot)."
        echo "              logs: bash linux_start.sh service logs   |   status: bash linux_start.sh service status"
        echo "              iniciar/parar/reiniciar: sudo systemctl start|stop|restart whatsbot-lite"
        echo "              voltar ao modo terminal: bash linux_start.sh service uninstall (a pergunta volta na próxima vez)"
        exit 0
    fi
    if [ "$choice" != "disabled" ] && [ -t 0 ] && [ -d /run/systemd/system ]; then
        echo
        echo "========================================"
        echo "  Como rodar o WhatsBot-Lite?"
        echo "========================================"
        echo
        echo "[1] Serviço - inicia sozinho quando o servidor liga, em segundo plano (pede sudo)"
        echo "[2] Somente terminal - roda só enquanto este terminal estiver aberto"
        echo
        ans=""
        while [ "$ans" != "1" ] && [ "$ans" != "2" ]; do
            read -r -p "Escolha uma opção [1/2]: " ans || exit 1
        done
        mkdir -p storages
        if [ "$ans" = "2" ]; then
            echo disabled > "$CHOICE_FILE"
            echo "[linux_start] OK: o WhatsBot-Lite vai rodar somente neste terminal."
        elif bash "$(pwd)/linux_start.sh" service install; then
            exit 0
        else
            # Não segue para o modo terminal: o free_port abaixo mataria quem ocupa a porta (motivo da recusa).
            echo "[linux_start] Serviço NÃO instalado. Resolva o erro acima e rode de novo (a pergunta volta)." >&2
            exit 1
        fi
    fi
fi

# Versão do GOWA. Fonte única: o arquivo GOWA_VERSION na raiz do repo, o mesmo
# lido pelo Dockerfile e por gowa/binary.py. Override por env se precisar.
GOWA_VERSION="${GOWA_VERSION:-$(cat ./GOWA_VERSION 2>/dev/null || echo 9.6.0)}"

require_cmd() {
    if ! command -v "$1" >/dev/null 2>&1; then
        echo "[linux_start] ERRO: '$1' não encontrado. Instale com o gerenciador de pacotes da sua distro."
        return 1
    fi
}

# ===== Python + venv =====
if [ ! -x ./venv/bin/python ]; then
    require_cmd python3 || exit 1
    echo "[linux_start] criando venv em ./venv ..."
    python3 -m venv venv
fi
echo "[linux_start] sincronizando dependências (pip install -r requirements.txt)..."
./venv/bin/pip install -q -r requirements.txt

# ===== GOWA Linux binary =====
# Equivalente ao Dockerfile: baixa a release oficial do go-whatsapp-web-multidevice
# se o binário local não existir. .gitignore exclui bin/gowa — cada máquina
# baixa o seu na 1ª execução.
#
# ``bin/.gowa_stamp`` registra qual versão foi baixada. Quando um ``git pull``
# bumpa GOWA_VERSION, o stamp diverge e o binário é re-baixado. Sem isso, uma
# máquina de dev ficaria presa na versão da primeira execução para sempre.
if [ ! -x ./bin/gowa ] || [ "$(cat bin/.gowa_stamp 2>/dev/null)" != "$GOWA_VERSION" ]; then
    require_cmd curl  || exit 1
    require_cmd unzip || exit 1
    case "$(uname -m)" in
        x86_64|amd64)  TARGETARCH="amd64" ;;
        aarch64|arm64) TARGETARCH="arm64" ;;
        *) echo "[linux_start] arquitetura $(uname -m) não suportada pelo GOWA"; exit 1 ;;
    esac
    GOWA_URL="https://github.com/aldinokemal/go-whatsapp-web-multidevice/releases/download/v${GOWA_VERSION}/whatsapp_${GOWA_VERSION}_linux_${TARGETARCH}.zip"
    echo "[linux_start] baixando GOWA v${GOWA_VERSION} (linux-${TARGETARCH})..."
    mkdir -p bin
    tmpdir=$(mktemp -d)
    trap 'rm -rf "$tmpdir"' EXIT
    if ! curl -fsSL "$GOWA_URL" -o "$tmpdir/gowa.zip"; then
        echo "[linux_start] ERRO: falha ao baixar $GOWA_URL"
        exit 1
    fi
    unzip -q "$tmpdir/gowa.zip" -d "$tmpdir/extract"
    if [ ! -f "$tmpdir/extract/linux-${TARGETARCH}" ]; then
        echo "[linux_start] ERRO: arquivo linux-${TARGETARCH} não encontrado dentro do zip do GOWA"
        ls -la "$tmpdir/extract" || true
        exit 1
    fi
    cp "$tmpdir/extract/linux-${TARGETARCH}" ./bin/gowa
    chmod +x ./bin/gowa
    echo "$GOWA_VERSION" > ./bin/.gowa_stamp
    rm -rf "$tmpdir"
    trap - EXIT
    echo "[linux_start] GOWA v${GOWA_VERSION} pronto em ./bin/gowa"
fi

# Libera as portas que vamos usar — equivalente ao taskkill do
# ``windows_start.bat``. Mata QUALQUER processo escutando nas portas alvo
# (uvicorn antigo nosso, instância de outro workspace, qualquer coisa) e
# também gowa órfão deste diretório.
free_port() {
    local port="$1"
    if command -v fuser >/dev/null 2>&1; then
        fuser -k -TERM "${port}/tcp" 2>/dev/null || true
    elif command -v lsof >/dev/null 2>&1; then
        local pids
        pids=$(lsof -ti "tcp:${port}" 2>/dev/null || true)
        if [ -n "$pids" ]; then
            kill -TERM $pids 2>/dev/null || true
        fi
    else
        echo "[linux_start] aviso: nem 'fuser' nem 'lsof' encontrados; não consigo liberar :$port"
    fi
}
echo "[linux_start] $(date '+%H:%M:%S') liberando portas $WHATSBOT_WEB_PORT (web) e $WHATSBOT_GOWA_PORT (gowa)..."
free_port "$WHATSBOT_WEB_PORT"
free_port "$WHATSBOT_GOWA_PORT"
pkill -f "$(pwd)/bin/gowa" 2>/dev/null || true
sleep 1

# uvicorn valida cada --reload-dir antes de subir; numa instalação nova
# storages/plugins ainda não existe (é criada em runtime por create_app).
mkdir -p storages/plugins

uvicorn_args=(--host 0.0.0.0 --port "$WHATSBOT_WEB_PORT" --log-level warning)
if [ "$WHATSBOT_RELOAD" = "0" ]; then
    mode_label="(sem hot-reload — produção)"
else
    mode_label="--reload"
    uvicorn_args+=(
        --reload
        --reload-dir server
        --reload-dir agent
        --reload-dir config
        --reload-dir gowa
        --reload-dir db
        --reload-dir plugins
        --reload-dir storages/plugins
    )
fi

# Modo serviço (systemd, ver linux_service.sh): sem o loop. O ``exec`` faz o
# uvicorn virar o processo principal da unit; quando ele sai (crash, ou o
# ``os._exit`` do restart de plugin/self-update) o ``Restart=always`` refaz ESTE
# script inteiro. Assim o pip e o GOWA são reconferidos a cada subida; o loop
# abaixo pularia isso e subiria com as dependências antigas após um update.
if [ "${WHATSBOT_SERVICE:-0}" = "1" ]; then
    echo "[linux_start] $(date '+%H:%M:%S') starting uvicorn $mode_label (port $WHATSBOT_WEB_PORT) como serviço..."
    exec ./venv/bin/python -m uvicorn server.dev:app "${uvicorn_args[@]}"
fi

while true; do
    echo "[linux_start] $(date '+%H:%M:%S') starting uvicorn $mode_label (port $WHATSBOT_WEB_PORT)..."
    ./venv/bin/python -m uvicorn server.dev:app "${uvicorn_args[@]}"
    rc=$?
    echo "[linux_start] $(date '+%H:%M:%S') uvicorn exited rc=$rc"
    # Mata GOWA orphan antes de relançar (senão a porta fica presa)
    pkill -f "$(pwd)/bin/gowa" 2>/dev/null || true
    sleep 2
done
