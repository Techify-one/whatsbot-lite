#!/bin/bash
# WhatsBot-Lite — macOS native launcher (hot-reload).
#
# Equivalente do ``windows_start.bat`` para usuários de macOS: configura o
# ambiente sozinho (instala Python se faltar, baixa o binário GOWA, cria a
# venv, instala as dependências) e sobe o servidor com ``uvicorn --reload``.
#
# Salvo com a extensão ``.command`` para poder ser aberto com duplo-clique
# pelo Finder. Para parar, feche a janela do Terminal ou rode
# ``macos_stop.command`` (ou Ctrl+C nesta janela).
#
# Para o build prod-like via Docker use ``./docker_start.sh``.
set -u

# ===== Porta web (frontend + REST + WS). Sobrescrevível externamente. =====
WEB_PORT="${WHATSBOT_WEB_PORT:-8080}"

# Versao do GOWA. Fonte unica: o arquivo GOWA_VERSION na raiz do repo, o mesmo
# lido pelo Dockerfile e por gowa/binary.py. Resolvido antes do cd, entao usa
# dirname "$0". Override por env se precisar.
GOWA_VERSION="${GOWA_VERSION:-$(cat "$(dirname "$0")/GOWA_VERSION" 2>/dev/null || echo 9.6.0)}"
# Versão do Python instalada automaticamente quando nenhuma 3.11+ é encontrada.
PY_VERSION="${PY_VERSION:-3.12.8}"

# Garante que o diretório de trabalho é o do script (idem ``cd /d %~dp0``).
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

echo ""
echo "========================================"
echo "  WhatsBot-Lite - Verificando ambiente..."
echo "========================================"
echo ""

# ===== 0. ENCERRAR PROCESSOS ANTERIORES =====
# Mata qualquer coisa escutando na porta web e o GOWA órfão deste diretório.
free_port() {
    local pids
    pids=$(lsof -ti "tcp:$1" 2>/dev/null || true)
    if [ -n "$pids" ]; then
        kill -TERM $pids 2>/dev/null || true
        sleep 1
        pids=$(lsof -ti "tcp:$1" 2>/dev/null || true)
        [ -n "$pids" ] && kill -KILL $pids 2>/dev/null || true
    fi
}
free_port "$WEB_PORT"
pkill -f "$SCRIPT_DIR/bin/gowa" 2>/dev/null || true

# ===== 1. DETECTAR OU INSTALAR PYTHON >= 3.11 =====
PYTHON_CMD=""
py_ok() {
    # Executa o Python: se rodar e for >= 3.11, está apto.
    "$1" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 11) else 1)' \
        >/dev/null 2>&1
}

for cand in \
    python3.13 python3.12 python3.11 python3 \
    /Library/Frameworks/Python.framework/Versions/3.13/bin/python3 \
    /Library/Frameworks/Python.framework/Versions/3.12/bin/python3 \
    /Library/Frameworks/Python.framework/Versions/3.11/bin/python3 \
    /opt/homebrew/bin/python3 /usr/local/bin/python3
do
    resolved=$(command -v "$cand" 2>/dev/null || true)
    [ -n "$resolved" ] || { [ -x "$cand" ] && resolved="$cand"; }
    if [ -n "$resolved" ] && py_ok "$resolved"; then
        PYTHON_CMD="$resolved"
        break
    fi
done

if [ -z "$PYTHON_CMD" ]; then
    echo "[!] Python 3.11+ nao encontrado no sistema."
    echo "    Baixando e instalando o Python ${PY_VERSION} automaticamente..."
    echo "    Isso pode levar alguns minutos, aguarde..."
    echo ""

    PY_PKG="$SCRIPT_DIR/python_installer.pkg"
    PY_URL="https://www.python.org/ftp/python/${PY_VERSION}/python-${PY_VERSION}-macos11.pkg"

    if ! curl -fsSL "$PY_URL" -o "$PY_PKG"; then
        echo ""
        echo "[ERRO] Falha ao baixar o Python."
        echo "       Verifique sua conexao com a internet e tente novamente."
        echo "       Ou instale manualmente: https://www.python.org/downloads/macos/"
        echo ""
        rm -f "$PY_PKG"
        read -r -p "Pressione ENTER para sair..." _
        exit 1
    fi

    echo "    Instalando o Python ${PY_VERSION} — o macOS vai pedir sua senha."
    echo ""
    if ! sudo installer -pkg "$PY_PKG" -target /; then
        echo ""
        echo "[ERRO] Falha ao instalar o Python."
        echo "       Tente instalar manualmente: https://www.python.org/downloads/macos/"
        echo ""
        rm -f "$PY_PKG"
        read -r -p "Pressione ENTER para sair..." _
        exit 1
    fi
    rm -f "$PY_PKG"

    PY_MM="${PY_VERSION%.*}"   # 3.12.8 -> 3.12
    PYTHON_CMD="/Library/Frameworks/Python.framework/Versions/${PY_MM}/bin/python3"

    # Instala os certificados raiz (o Python do python.org nao vem com eles).
    CERT_CMD="/Applications/Python ${PY_MM}/Install Certificates.command"
    [ -f "$CERT_CMD" ] && "$CERT_CMD" >/dev/null 2>&1 || true

    if ! py_ok "$PYTHON_CMD"; then
        echo ""
        echo "[ERRO] Python foi instalado mas nao esta funcionando corretamente."
        echo "       Reinicie o Mac e rode o macos_start.command novamente."
        echo ""
        read -r -p "Pressione ENTER para sair..." _
        exit 1
    fi
    echo "    Python instalado com sucesso!"
    echo ""
fi

PY_VER=$("$PYTHON_CMD" -c 'import platform; print(platform.python_version())' 2>/dev/null)
echo "[OK] Python ${PY_VER} encontrado."

# ===== 2. VERIFICAR PIP =====
if ! "$PYTHON_CMD" -m pip --version >/dev/null 2>&1; then
    echo "[!] pip nao encontrado. Tentando recuperar com ensurepip..."
    "$PYTHON_CMD" -m ensurepip --upgrade >/dev/null 2>&1 || true
    if ! "$PYTHON_CMD" -m pip --version >/dev/null 2>&1; then
        echo ""
        echo "[ERRO] pip nao disponivel."
        echo "       Reinstale o Python: https://www.python.org/downloads/macos/"
        echo ""
        read -r -p "Pressione ENTER para sair..." _
        exit 1
    fi
fi
echo "[OK] pip disponivel."

# ===== 3. VERIFICAR / BAIXAR O BINARIO GOWA (macOS) =====
# .gitignore exclui bin/gowa — cada Mac baixa a release oficial na 1a execucao.
# bin/.gowa_stamp registra a versao baixada: se um git pull bumpar GOWA_VERSION,
# o stamp diverge e o binario e re-baixado.
if [ ! -x ./bin/gowa ] || [ "$(cat bin/.gowa_stamp 2>/dev/null)" != "$GOWA_VERSION" ]; then
    case "$(uname -m)" in
        arm64)         GOWA_ARCH="arm64" ;;
        x86_64|amd64)  GOWA_ARCH="amd64" ;;
        *) echo "[ERRO] Arquitetura $(uname -m) nao suportada pelo GOWA."
           read -r -p "Pressione ENTER para sair..." _; exit 1 ;;
    esac
    GOWA_URL="https://github.com/aldinokemal/go-whatsapp-web-multidevice/releases/download/v${GOWA_VERSION}/whatsapp_${GOWA_VERSION}_darwin_${GOWA_ARCH}.zip"
    echo "[!] Binario GOWA nao encontrado. Baixando v${GOWA_VERSION} (darwin-${GOWA_ARCH})..."
    mkdir -p bin
    tmpdir=$(mktemp -d)
    if ! curl -fsSL "$GOWA_URL" -o "$tmpdir/gowa.zip"; then
        echo ""
        echo "[ERRO] Falha ao baixar o GOWA de: $GOWA_URL"
        echo "       Verifique sua conexao com a internet e tente novamente."
        echo ""
        rm -rf "$tmpdir"
        read -r -p "Pressione ENTER para sair..." _
        exit 1
    fi
    unzip -q "$tmpdir/gowa.zip" -d "$tmpdir/extract"
    # O zip traz o binario como darwin-<arch> (+ readme.md); achamos por nome
    # e, em ultimo caso, pelo maior arquivo extraido.
    gowa_bin="$tmpdir/extract/darwin-${GOWA_ARCH}"
    if [ ! -f "$gowa_bin" ]; then
        gowa_bin=$(find "$tmpdir/extract" -type f ! -iname '*.md' \
                   -exec ls -S {} + 2>/dev/null | head -n 1)
    fi
    if [ -z "$gowa_bin" ] || [ ! -f "$gowa_bin" ]; then
        echo "[ERRO] Nao encontrei o binario GOWA dentro do zip baixado."
        rm -rf "$tmpdir"
        read -r -p "Pressione ENTER para sair..." _
        exit 1
    fi
    cp "$gowa_bin" ./bin/gowa
    chmod +x ./bin/gowa
    # Remove a quarentena do Gatekeeper (binario baixado da internet seria
    # bloqueado por "developer cannot be verified").
    xattr -dr com.apple.quarantine ./bin/gowa 2>/dev/null || true
    echo "$GOWA_VERSION" > ./bin/.gowa_stamp
    rm -rf "$tmpdir"
    echo "[OK] GOWA v${GOWA_VERSION} pronto em ./bin/gowa"
else
    echo "[OK] gowa encontrado."
fi

# ===== 4. CONFIGURAR O AMBIENTE (venv + dependencias) =====
if [ ! -x ./venv/bin/python ]; then
    echo ""
    echo "Primeira execucao detectada. Configurando ambiente..."
    echo "Isso pode levar alguns minutos, aguarde..."
    echo ""
    if ! "$PYTHON_CMD" -m venv venv; then
        echo "[ERRO] Falha ao criar o ambiente virtual (venv)."
        read -r -p "Pressione ENTER para sair..." _
        exit 1
    fi
fi
echo "Verificando dependencias..."
if ! ./venv/bin/pip install -q -r requirements.txt; then
    echo "[ERRO] Falha ao instalar as dependencias (requirements.txt)."
    read -r -p "Pressione ENTER para sair..." _
    exit 1
fi
echo ""
echo "[OK] Ambiente pronto!"

# uvicorn valida cada --reload-dir antes de subir; numa instalacao nova
# storages/plugins ainda nao existe (e criada em runtime por create_app).
mkdir -p storages/plugins

# ===== 5. ABRIR O NAVEGADOR APOS ALGUNS SEGUNDOS =====
( sleep 6; open "http://127.0.0.1:${WEB_PORT}" >/dev/null 2>&1 ) &

# ===== 6. SUBIR O SERVIDOR (hot-reload) =====
echo ""
echo "========================================"
echo "  WhatsBot-Lite rodando em http://127.0.0.1:${WEB_PORT}"
echo "  Feche esta janela ou rode macos_stop.command para parar."
echo "========================================"
echo ""

export NO_COLOR=1
# Loop externo: cobre quando o uvicorn parent morre ou um plugin chama
# os._exit (enable/disable via UI dispara schedule_restart).
while true; do
    ./venv/bin/python -m uvicorn server.dev:app \
        --host 0.0.0.0 --port "$WEB_PORT" \
        --reload \
        --reload-dir server \
        --reload-dir agent \
        --reload-dir config \
        --reload-dir gowa \
        --reload-dir db \
        --reload-dir plugins \
        --reload-dir storages/plugins \
        --log-level warning
    rc=$?
    echo "[macos_start] uvicorn encerrou (rc=$rc)"
    # Mata GOWA orfao antes de relancar (senao a porta fica presa).
    pkill -f "$SCRIPT_DIR/bin/gowa" 2>/dev/null || true
    sleep 2
done
