@echo off
:: ============================================================
:: Fazenda Radar — Inicializador para Windows
:: Duplo clique neste arquivo para abrir o painel no navegador.
:: ============================================================
title Fazenda Radar

echo.
echo ============================================
echo   FAZENDA RADAR — Iniciando...
echo ============================================
echo.

:: Move to script directory
cd /d "%~dp0"

:: ── 1. Find Python ────────────────────────────────────────────
set PYTHON=
for %%P in (python3 python) do (
    where %%P >nul 2>&1
    if not errorlevel 1 (
        set PYTHON=%%P
        goto :found_python
    )
)

:not_found
echo ERRO: Python nao encontrado.
echo Instale Python 3.9+ em https://python.org/downloads
echo Marque "Add Python to PATH" durante a instalacao.
echo.
pause
exit /b 1

:found_python
for /f "tokens=*" %%V in ('%PYTHON% --version 2^>^&1') do echo Python encontrado: %%V

:: ── 2. Create virtualenv if missing ───────────────────────────
set VENV_DIR=%~dp0.venv
if not exist "%VENV_DIR%\Scripts\python.exe" (
    echo Criando ambiente virtual...
    %PYTHON% -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo ERRO: Falha ao criar ambiente virtual.
        pause
        exit /b 1
    )
)

set VENV_PYTHON=%VENV_DIR%\Scripts\python.exe
set VENV_PIP=%VENV_DIR%\Scripts\pip.exe
set VENV_STREAMLIT=%VENV_DIR%\Scripts\streamlit.exe

:: ── 3. Install/upgrade dependencies ───────────────────────────
echo Verificando dependencias...
"%VENV_PIP%" install --quiet --upgrade pip
"%VENV_PIP%" install --quiet -r "%~dp0requirements.txt"
if errorlevel 1 (
    echo ERRO: Falha ao instalar dependencias.
    echo Verifique sua conexao com a internet.
    pause
    exit /b 1
)

:: ── 3b. Install Playwright browser (only if not already installed) ─────────
set PLAYWRIGHT_MARKER=%VENV_DIR%\.playwright_installed
if not exist "%PLAYWRIGHT_MARKER%" (
    echo Instalando navegador Playwright (Chromium) — somente na primeira vez...
    "%VENV_DIR%\Scripts\playwright.exe" install chromium
    if not errorlevel 1 (
        type nul > "%PLAYWRIGHT_MARKER%"
    ) else (
        echo AVISO: Falha ao instalar Chromium. e-leiloes.com.br nao estara disponivel.
    )
)

echo.
echo Dependencias OK. Abrindo painel no navegador...
echo (Feche esta janela para encerrar o servidor)
echo.

:: ── 4. Launch Streamlit ───────────────────────────────────────
"%VENV_STREAMLIT%" run "%~dp0dashboard\app.py" ^
    --server.port 8501 ^
    --server.headless false ^
    --browser.gatherUsageStats false ^
    --theme.primaryColor "#1a7f4b" ^
    --theme.backgroundColor "#f8faf8" ^
    --theme.secondaryBackgroundColor "#e8f0e8" ^
    --theme.textColor "#1a1a1a"

echo.
echo Servidor encerrado.
pause
