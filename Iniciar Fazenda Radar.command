#!/bin/bash
# ============================================================
# Fazenda Radar — Inicializador para macOS
# Duplo clique neste arquivo para abrir o painel no navegador.
# ============================================================

# Move to script directory (wherever the user placed the folder)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo ""
echo "============================================"
echo "  FAZENDA RADAR — Iniciando..."
echo "============================================"
echo ""

# ── 1. Find Python ────────────────────────────────────────────
PYTHON=""
for candidate in python3 python3.11 python3.10 python3.9 python; do
    if command -v "$candidate" &>/dev/null; then
        PYTHON="$candidate"
        break
    fi
done

if [ -z "$PYTHON" ]; then
    osascript -e 'display alert "Python não encontrado" message "Instale Python 3.9+ em python.org/downloads antes de continuar." as critical'
    exit 1
fi

echo "Python encontrado: $($PYTHON --version)"

# ── 2. Create virtualenv if missing ───────────────────────────
VENV_DIR="$SCRIPT_DIR/.venv"
if [ ! -d "$VENV_DIR" ]; then
    echo "Criando ambiente virtual..."
    "$PYTHON" -m venv "$VENV_DIR"
    if [ $? -ne 0 ]; then
        echo "ERRO: Falha ao criar virtualenv."
        read -p "Pressione Enter para fechar."
        exit 1
    fi
fi

VENV_PYTHON="$VENV_DIR/bin/python"
VENV_PIP="$VENV_DIR/bin/pip"
VENV_STREAMLIT="$VENV_DIR/bin/streamlit"

# ── 3. Install/upgrade dependencies ───────────────────────────
echo "Verificando dependências..."
"$VENV_PIP" install --quiet --upgrade pip
"$VENV_PIP" install --quiet -r "$SCRIPT_DIR/requirements.txt"

if [ $? -ne 0 ]; then
    echo "ERRO: Falha ao instalar dependências."
    echo "Verifique sua conexão com a internet e tente novamente."
    read -p "Pressione Enter para fechar."
    exit 1
fi

# ── 3b. Install Playwright browser (only if not already installed) ─────────
PLAYWRIGHT_MARKER="$VENV_DIR/.playwright_installed"
if [ ! -f "$PLAYWRIGHT_MARKER" ]; then
    echo "Instalando navegador Playwright (Chromium) — somente na primeira vez..."
    "$VENV_DIR/bin/playwright" install chromium
    if [ $? -eq 0 ]; then
        touch "$PLAYWRIGHT_MARKER"
    else
        echo "AVISO: Falha ao instalar Chromium. e-leiloes.com.br não estará disponível."
    fi
fi

echo ""
echo "Dependências OK. Abrindo painel..."
echo "(Feche esta janela para encerrar o servidor)"
echo ""

# ── 4. Launch Streamlit ───────────────────────────────────────
"$VENV_STREAMLIT" run "$SCRIPT_DIR/dashboard/app.py" \
    --server.port 8501 \
    --server.headless false \
    --browser.gatherUsageStats false \
    --theme.primaryColor "#1a7f4b" \
    --theme.backgroundColor "#f8faf8" \
    --theme.secondaryBackgroundColor "#e8f0e8" \
    --theme.textColor "#1a1a1a"

echo ""
echo "Servidor encerrado."
